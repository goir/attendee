from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from bots.models import WebhookDeliveryAttempt, WebhookDeliveryAttemptStatus, WebhookTriggerTypes
from bots.tasks.deliver_webhook_task import deliver_webhook


def _parse_boundary(value, *, end_of_day):
    """Parse an ISO datetime or a YYYY-MM-DD date into an aware datetime."""
    dt = parse_datetime(value)
    if dt is None:
        d = parse_date(value)
        if d is None:
            raise CommandError(f"Could not parse date/datetime: {value!r}. Use YYYY-MM-DD or ISO 8601.")
        hour, minute, second = (23, 59, 59) if end_of_day else (0, 0, 0)
        dt = timezone.datetime(d.year, d.month, d.day, hour, minute, second)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class Command(BaseCommand):
    help = (
        "Re-enqueue the 'async_transcription.state_change' (state=complete) webhook delivery attempts so they are "
        "redelivered. The destination URL is read fresh from the subscription at send time, so update the "
        "subscription URL first (see update_webhook_subscription_urls), then run this. "
        "Defaults to a preview; pass --execute to actually resend."
    )

    def add_arguments(self, parser):
        parser.add_argument("--project", type=str, default=None, help="Scope to a single project object_id (proj_...).")
        parser.add_argument("--bot", action="append", dest="bots", default=[], help="Bot object_id (bot_...). Repeatable.")
        parser.add_argument("--transcription", action="append", dest="transcriptions", default=[], help="Async transcription object_id (tran_...). Repeatable.")
        parser.add_argument("--since", type=str, default=None, help="Only attempts created on/after this date (YYYY-MM-DD or ISO).")
        parser.add_argument("--until", type=str, default=None, help="Only attempts created on/before this date (YYYY-MM-DD or ISO).")
        parser.add_argument(
            "--status",
            choices=["all", "failure", "success", "pending"],
            default="all",
            help="Filter by original delivery status (default: all).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Cap the number of attempts resent in this run.")
        parser.add_argument(
            "--keep-attempt-count",
            action="store_true",
            help="Do not reset attempt_count. By default it is reset to 0 so the retry budget is restored.",
        )
        parser.add_argument("--execute", action="store_true", help="Actually resend. Without this flag the command only previews.")

    def _build_queryset(self, options):
        qs = WebhookDeliveryAttempt.objects.filter(
            webhook_trigger_type=WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE,
            payload__state="complete",
        ).select_related("webhook_subscription", "bot")

        if options["project"]:
            qs = qs.filter(webhook_subscription__project__object_id=options["project"])
        if options["bots"]:
            qs = qs.filter(bot__object_id__in=options["bots"])
        if options["transcriptions"]:
            qs = qs.filter(payload__id__in=options["transcriptions"])
        if options["since"]:
            qs = qs.filter(created_at__gte=_parse_boundary(options["since"], end_of_day=False))
        if options["until"]:
            qs = qs.filter(created_at__lte=_parse_boundary(options["until"], end_of_day=True))

        status_map = {
            "failure": WebhookDeliveryAttemptStatus.FAILURE,
            "success": WebhookDeliveryAttemptStatus.SUCCESS,
            "pending": WebhookDeliveryAttemptStatus.PENDING,
        }
        if options["status"] in status_map:
            qs = qs.filter(status=status_map[options["status"]])

        qs = qs.order_by("created_at")
        if options["limit"]:
            qs = qs[: options["limit"]]
        return qs

    def handle(self, *args, **options):
        attempts = list(self._build_queryset(options))

        if not attempts:
            self.stdout.write(self.style.WARNING("No matching 'async_transcription.state_change' (complete) delivery attempts found."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"{len(attempts)} delivery attempt(s) matched:"))
        preview = attempts[:50]
        for a in preview:
            bot_id = a.bot.object_id if a.bot_id else "-"
            tran_id = (a.payload or {}).get("id", "-")
            status = WebhookDeliveryAttemptStatus(a.status).label
            self.stdout.write(f"  {a.idempotency_key}  bot={bot_id}  tran={tran_id}  status={status}  attempts={a.attempt_count}\n    -> {a.webhook_subscription.url}  ({a.created_at:%Y-%m-%d %H:%M})")
        if len(attempts) > len(preview):
            self.stdout.write(f"  ... and {len(attempts) - len(preview)} more")

        if not options["execute"]:
            self.stdout.write(self.style.WARNING("Preview only. Re-run with --execute to resend these webhooks."))
            return

        reset_attempts = not options["keep_attempt_count"]
        resent = 0
        for a in attempts:
            update_fields = ["status", "updated_at"]
            a.status = WebhookDeliveryAttemptStatus.PENDING
            if reset_attempts:
                a.attempt_count = 0
                update_fields.append("attempt_count")
            a.save(update_fields=update_fields)
            deliver_webhook.delay(a.id)
            resent += 1

        self.stdout.write(self.style.SUCCESS(f"Re-enqueued {resent} webhook delivery attempt(s) for redelivery."))
