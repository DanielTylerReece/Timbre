# page.py
#
# Copyright 2023 Nokse
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Base navigation page (Adw.NavigationPage + IDisconnectable).

Lifecycle (upstream Page pattern, adapted to ``utils.run_async``):
    ``load()`` -> worker ``_load_async()`` (db reads only — NEVER the network) ->
    idle ``_load_finish()`` (build widgets). The window pops call
    ``disconnect_all`` so every page + its child widgets release their signals.
"""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from ..widgets import HTCarouselWidget, HTGenericTrackWidget

logger = logging.getLogger(__name__)


class Page(Adw.NavigationPage, IDisconnectable):
    """Base class for all Timbre browse pages."""

    __gtype_name__ = "Page"

    id = None

    @classmethod
    def new_from_id(cls, id):
        instance = cls()
        instance.id = id
        return instance

    def __init__(self):
        IDisconnectable.__init__(self)
        super().__init__()

        # Per-build signals for swappable sections (decade/search). Section
        # builders record More-button / row-activated / chip handlers here when
        # passed ``scope=self._section_signals`` so in-place rebuilds can sweep
        # them without growing ``self.signals``. Swept by both teardown_sections
        # (per rebuild) and disconnect_all (at pop) so nothing survives the page.
        self._section_signals = []

        self.set_title(_("Loading..."))

        self.builder = Gtk.Builder.new_from_resource(
            "/io/github/tylerreece/timbre/ui/pages_ui/page_template.ui"
        )

        self.content = self.builder.get_object("_content")
        self.content_stack = self.builder.get_object("_content_stack")
        self.object = self.builder.get_object("_main")
        self.scrolled_window = self.builder.get_object("_scrolled_window")

        self.set_child(self.object)

    def disconnect_all(self, *args):
        """Disconnect per-build section signals, then the base IDisconnectable
        set. Pages that rebuild sections in place park their More-button / row /
        chip handlers in ``_section_signals`` (not ``self.signals``); without
        this sweep those would survive pop and pin the page via the bound-method
        and closure references they hold.
        """
        scope = getattr(self, "_section_signals", None)
        if scope:
            for obj, signal_id in scope:
                if obj.handler_is_connected(signal_id):
                    obj.disconnect(signal_id)
            scope.clear()
        IDisconnectable.disconnect_all(self, *args)

    def load(self):
        """Load page content: db reads on a worker, widget build on idle."""

        def work():
            self._load_async()
            return True

        def done(_result):
            self._load_finish()
            self.content_stack.set_visible_child_name("content")

        def on_error(exc):
            logger.exception("Error while loading page", exc_info=exc)
            self._show_error_state()

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)
        return self

    def _show_error_state(self) -> None:
        """Replace the perpetual spinner with an error + Retry status page.

        Retry re-runs ``load()`` (which itself swaps back to the spinner stack
        child first). Built once and cached so repeated failures reuse it.
        """
        status = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title=_("Couldn't load"),
            description=_("Something went wrong loading this page."),
        )
        button = Gtk.Button(
            label=_("Retry"), halign=Gtk.Align.CENTER,
            css_classes=["pill", "suggested-action"],
        )
        self.signals.append((button, button.connect("clicked", self._on_retry)))
        status.set_child(button)

        # Drop any previous error child so we don't stack them on re-failure.
        if getattr(self, "_error_box", None) is not None:
            self.content_stack.remove(self._error_box)
        self._error_box = status
        self.content_stack.add_named(status, "error")
        self.content_stack.set_visible_child_name("error")

    def _on_retry(self, _button) -> None:
        self.content_stack.set_visible_child_name("loading")
        self.load()

    def _load_async(self) -> None:
        """Fetch page data from SQLite on a worker thread (override)."""
        raise NotImplementedError

    def _load_finish(self) -> None:
        """Build the page widgets on the main loop (override)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Content helpers                                                    #
    # ------------------------------------------------------------------ #

    def append(self, widget) -> None:
        if isinstance(widget, IDisconnectable):
            self.disconnectables.append(widget)
        self.content.append(widget)

    def new_carousel_for(self, title, items, more_function=None,
                         item_type="card", year_function=None, scope=None):
        """Append a carousel of cards for ``items`` (skipped when empty).

        ``year_function`` (``fn(year, offset, limit)``) makes the carousel's
        More button push a year-filtered list (Phase 7 headline feature) instead
        of a plain paged list.

        ``scope`` (a signals list) is where directly-tracked handlers are
        recorded; defaults to ``self.signals``. Pages that rebuild sections
        in place (decade/search) pass a per-build scope they disconnect+clear
        on teardown so stale entries don't accumulate across rebuilds. The
        carousel widget owns its own signals via ``self.disconnectables``, so
        this builder records nothing here today — the param keeps the API
        symmetric with ``new_track_list_for``.
        """
        if not items:
            return
        carousel = HTCarouselWidget(title)
        carousel.item_type = item_type
        carousel.set_items(items)
        if year_function is not None:
            carousel.set_year_function(year_function)
        elif more_function:
            carousel.set_more_function(more_function)
        self.append(carousel)

    def new_track_list_for(self, title, tracks, more_function=None,
                           scope=None, more_title=None):
        """Append a titled list of track rows (skipped when empty).

        ``more_function`` (``offset, limit -> list``) makes a More button push a
        full from-function track-list page. ``more_title`` overrides the pushed
        page's title (defaults to the section ``title``) — e.g. the artist page
        shows a "Popular" section inline but pushes "{Artist} — Popular".

        ``scope`` (a signals list) is where the More-button + row-activated
        handlers are recorded; defaults to ``self.signals``. Pages that rebuild
        this section in place (decade/search) pass a per-build scope they
        disconnect+clear on teardown, so toggling a filter doesn't accrete
        stale ``self.signals`` entries pinning detached list boxes + captured
        row lists until the page pops.
        """
        if not tracks:
            return

        signals = self.signals if scope is None else scope

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12)
        header = Gtk.Box(margin_bottom=6)
        header.append(Gtk.Label(label=title, xalign=0, hexpand=True,
                                css_classes=["title-3"], ellipsize=3))
        if more_function:
            more_btn = Gtk.Button(label=_("More"), halign=Gtk.Align.END,
                                  valign=Gtk.Align.CENTER,
                                  css_classes=["small-pill"])
            signals.append((
                more_btn,
                more_btn.connect(
                    "clicked", self._on_track_list_more,
                    more_title or title, more_function
                ),
            ))
            header.append(more_btn)
        box.append(header)

        list_box = Gtk.ListBox(css_classes=["tracks-list-box"],
                               selection_mode=Gtk.SelectionMode.NONE)
        rows = list(tracks)
        for index, track in enumerate(rows):
            row = HTGenericTrackWidget(track)
            self.disconnectables.append(row)
            row.index = index
            list_box.append(row)
        signals.append((
            list_box,
            list_box.connect(
                "row-activated",
                lambda _lb, r: utils.player_object.play_this(rows, r.index),
            ),
        ))
        box.append(list_box)
        self.content.append(box)

    def _on_track_list_more(self, _btn, title, more_function):
        from .from_function_page import HTFromFunctionPage

        page = HTFromFunctionPage(title, item_type="track")
        page.set_function(more_function)
        page.load()
        utils.navigation_view.push(page)

    # ------------------------------------------------------------------ #
    # In-place section rebuild helpers (decade/search)                   #
    # ------------------------------------------------------------------ #

    def reparent_section(self, parent, build_fn):
        """Run a section builder that appends to ``self.content``, then move the
        just-appended child into ``parent`` (a swappable sections box).

        The base ``new_track_list_for`` / ``new_carousel_for`` append directly
        to ``self.content``; pages that rebuild sections in place want them
        inside their own box so a toggle/re-query can clear+rebuild just those.
        """
        before = self.content.get_last_child()
        build_fn()
        after = self.content.get_last_child()
        if after is not None and after is not before:
            self.content.remove(after)
            parent.append(after)

    def teardown_sections(self, box, scope):
        """Tear down the section widgets in ``box`` and their tracked signals.

        Disconnects+drops every child IDisconnectable on ``self.disconnectables``
        (track rows / carousels the base builders pushed), disconnects+clears the
        ``scope`` signals list (per-build More-button / row-activated handlers),
        then empties ``box``. ``scope`` is the per-build signals list the page
        passed to the section builders — disconnecting it here is what stops
        stale entries accruing across rebuilds.
        """
        for d in list(self.disconnectables):
            if hasattr(d, "disconnect_all"):
                d.disconnect_all()
            self.disconnectables.remove(d)
        for obj, signal_id in scope:
            if obj.handler_is_connected(signal_id):
                obj.disconnect(signal_id)
        scope.clear()
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()
