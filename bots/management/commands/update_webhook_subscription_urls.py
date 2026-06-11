from django.core.management.base import BaseCommand, CommandError

from bots.models import WebhookSubscription, WebhookTriggerTypes


class Command(BaseCommand):
    help = (
        "List webhook subscriptions, or bulk-update their destination URL. "
        "The URL is read fresh from the WebhookSubscription at delivery time, so changing it here "
        "is all that is needed to point existing bots (and any re-sent events) at the new endpoint."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="List all webhook subscriptions (id, project, bot, url, active, triggers) and exit.",
        )
        parser.add_argument(
            "--old",
            type=str,
            default=None,
            help="Exact current URL to replace. Use --list first to see exact values.",
        )
        parser.add_argument(
            "--new",
            type=str,
            default=None,
            help="New URL to set on every subscription whose url matches --old.",
        )
        parser.add_argument(
            "--project",
            type=str,
            default=None,
            help="Optional project object_id (proj_...) to scope the update/listing to a single project.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def _format_row(self, sub):
        target = sub.bot.object_id if sub.bot_id else "PROJECT-LEVEL"
        trigger_names = ", ".join(WebhookTriggerTypes(t).label for t in (sub.triggers or []))
        active = "active" if sub.is_active else "INACTIVE"
        return f"  {sub.object_id}  [{sub.project.name}]  {target}  {active}\n    url: {sub.url}\n    triggers: [{trigger_names}]"

    def handle(self, *args, **options):
        base_qs = WebhookSubscription.objects.select_related("project", "bot").order_by("project__name", "bot__object_id")
        if options["project"]:
            base_qs = base_qs.filter(project__object_id=options["project"])

        if options["list"]:
            count = base_qs.count()
            self.stdout.write(self.style.MIGRATE_HEADING(f"{count} webhook subscription(s):"))
            for sub in base_qs:
                self.stdout.write(self._format_row(sub))
            return

        old = options["old"]
        new = options["new"]
        if not (old and new):
            raise CommandError("Provide --list, or both --old and --new to perform a replacement.")

        qs = base_qs.filter(url=old)
        matches = list(qs)
        if not matches:
            self.stdout.write(self.style.WARNING(f"No subscriptions found with url == {old!r}. Run --list to see exact values."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"{len(matches)} subscription(s) will be updated to: {new}"))
        for sub in matches:
            self.stdout.write(self._format_row(sub))

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no changes written."))
            return

        updated = qs.update(url=new)
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} subscription(s)."))
