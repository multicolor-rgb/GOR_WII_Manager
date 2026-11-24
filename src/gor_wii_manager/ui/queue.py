"""Transfer queue panel shown at the bottom of the main window."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..core.i18n import _  # noqa: E402
from ..core.jobs import Job  # noqa: E402
from ..core.util import human_duration, human_speed  # noqa: E402


STATUS_LABELS = {
    "queued": "Oczekuje",
    "running": "W toku",
    "done": "Gotowe",
    "error": "Błąd",
    "cancelled": "Anulowano",
}


class QueueRow(Gtk.ListBoxRow):
    def __init__(self, job: Job, on_cancel):
        super().__init__()
        self.job = job
        self.set_selectable(False)
        self.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title = Gtk.Label(label=job.title, xalign=0.0, hexpand=True)
        self.title.set_ellipsize(3)
        self.title.set_text(job.title)
        top.append(self.title)
        if job.cancellable:
            cancel = Gtk.Button(icon_name="process-stop-symbolic")
            cancel.add_css_class("flat")
            cancel.set_tooltip_text(_("Anuluj"))
            cancel.connect("clicked", lambda *_: on_cancel(job))
            top.append(cancel)
        box.append(top)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(False)
        if job.total:
            self.progress.set_fraction(job.fraction)
        else:
            self.progress.pulse()
        box.append(self.progress)

        self.status = Gtk.Label(xalign=0.0)
        self.status.add_css_class("caption")
        self.status.add_css_class("dim-label")
        self.status.set_ellipsize(3)
        box.append(self.status)
        self.set_child(box)
        self.update()

    def update(self) -> None:
        job = self.job
        if job.total:
            self.progress.set_fraction(job.fraction)
        if job.status == "running":
            parts = [_(STATUS_LABELS["running"])]
            if job.speed:
                parts.append(human_speed(job.speed))
            if job.eta is not None:
                parts.append(
                    _("{time} pozostało").format(time=human_duration(job.eta))
                )
            if job.detail:
                parts.append(job.detail)
            self.status.set_text(" · ".join(parts))
        elif job.status == "error":
            self.status.set_text(_("Błąd: {error}").format(error=job.error))
            self.status.remove_css_class("dim-label")
            self.status.add_css_class("error")
        elif job.status == "cancelled":
            self.status.set_text(_("Anulowano"))
        else:
            self.status.set_text(
                _(STATUS_LABELS.get(job.status, job.status))
            )


class QueueView(Gtk.Revealer):
    def __init__(self, on_cancel, on_clear):
        super().__init__()
        self._on_cancel = on_cancel
        self._rows: dict[int, QueueRow] = {}
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_reveal_child(False)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("queue-panel")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(12)
        header.set_margin_end(8)
        header.set_margin_top(6)
        header.set_margin_bottom(2)
        title = Gtk.Label(label=_("Kolejka zadań"), xalign=0.0, hexpand=True)
        title.add_css_class("heading")
        header.append(title)
        self.count_label = Gtk.Label(xalign=1.0)
        self.count_label.add_css_class("dim-label")
        self.count_label.add_css_class("caption")
        header.append(self.count_label)
        clear = Gtk.Button(label=_("Wyczyść"))
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: on_clear())
        header.append(clear)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.set_tooltip_text(_("Ukryj kolejkę"))
        close.connect("clicked", lambda *_: self.set_reveal_child(False))
        header.append(close)
        panel.append(header)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(10)
        self.listbox.set_margin_end(10)
        self.listbox.set_margin_bottom(8)
        panel.append(self.listbox)
        self.set_child(panel)

    def update(self, jobs: list[Job]) -> None:
        visible = [job for job in jobs if job.active or job.finished_at][-12:]
        active = [job for job in jobs if job.active]
        if active:
            self.count_label.set_text(
                _("{count} w toku").format(count=len(active))
                if len(active) > 1
                else _("1 zadanie w toku")
            )
            self.set_reveal_child(True)
        else:
            self.count_label.set_text("")
        wanted = {job.id: job for job in visible}
        for child in list(self.listbox):
            self.listbox.remove(child)
        self._rows.clear()
        for job in visible:
            row = QueueRow(job, self._on_cancel)
            self._rows[job.id] = row
            self.listbox.append(row)


class QueueToggleButton(Gtk.ToggleButton):
    def __init__(self, queue_view: QueueView):
        super().__init__(icon_name="view-list-symbolic")
        self.queue = queue_view
        self.set_tooltip_text(_("Kolejka zadań"))
        self.connect("toggled", self._on_toggled)
        queue_view.connect("notify::reveal-child", self._sync)

    def _on_toggled(self, button: Gtk.ToggleButton) -> None:
        self.queue.set_reveal_child(button.get_active())

    def _sync(self, revealer, _param) -> None:
        self.handler_block_by_func(self._on_toggled)
        self.set_active(revealer.get_reveal_child())
        self.handler_unblock_by_func(self._on_toggled)
