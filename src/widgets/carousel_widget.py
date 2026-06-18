# carousel_widget.py
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

from typing import Callable

from gi.repository import Adw, GLib, GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from .card_widget import HTCardWidget

# Max cards rendered inline before the carousel offers a "More" page.
_INLINE_CARDS = 8


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/carousel_widget.ui"
)
class HTCarouselWidget(Gtk.Box, IDisconnectable):
    """Horizontal carousel of browse cards with prev/next arrows + More.

    Consumes the Phase 5 browse-item convention (see ``card_widget``); each
    item becomes an ``HTCardWidget``. ``set_more_function(fn)`` makes the More
    button push a ``HTFromFunctionPage`` driven by ``fn(offset, limit)``.
    """

    __gtype_name__ = "HTCarouselWidget"

    # Emitted when the (opt-in) refresh button is clicked. The page connects
    # via its tracked-signals list and drives the rebuild — the widget never
    # stores a bound page method (leak rule).
    __gsignals__ = {
        "refresh-clicked": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    title_label = Gtk.Template.Child()
    refresh_button = Gtk.Template.Child()
    next_button = Gtk.Template.Child()
    prev_button = Gtk.Template.Child()
    carousel_scrolled_window = Gtk.Template.Child()
    cards_box = Gtk.Template.Child()
    more_button = Gtk.Template.Child()

    def __init__(self, title=""):
        IDisconnectable.__init__(self)
        super().__init__()

        self.signals.append((
            self.next_button,
            self.next_button.connect("clicked", self.carousel_go_next),
        ))
        self.signals.append((
            self.prev_button,
            self.prev_button.connect("clicked", self.carousel_go_prev),
        ))
        self.signals.append((
            self.more_button,
            self.more_button.connect("clicked", self.on_more_clicked),
        ))
        self.signals.append((
            self.refresh_button,
            self.refresh_button.connect("clicked", self._on_refresh_clicked),
        ))

        self.title = title
        self.title_label.set_label(self.title)

        self.more_function = None
        self.items = []
        # card kind hint for the More page (cards vs track rows).
        self.item_type = "card"
        # Phase 7: when set, the More page renders a year-filter dropdown and
        # drives itself via this year-aware fetch ``fn(year, offset, limit)``.
        self.year_function = None

        adjustment = self.carousel_scrolled_window.get_hadjustment()
        self.signals.append((
            adjustment,
            adjustment.connect("value-changed", self._update_button_sensitivity),
        ))
        # The "value-changed" handler above only fires when the user scrolls.
        # The scrollable RANGE (upper/page-size) isn't known until the carousel
        # is allocated, which happens AFTER set_items' idle re-check; allocation
        # fires "changed" (not "value-changed"). Without listening here, a
        # carousel that overflows only after allocation keeps the next button
        # stuck insensitive (computed against upper==page_size==0 at idle time)
        # so mouse clicks on the arrows do nothing. Re-evaluate on "changed".
        self.signals.append((
            adjustment,
            adjustment.connect("changed", self._update_button_sensitivity),
        ))

    def set_more_function(self, function: Callable) -> None:
        """Set the ``(offset, limit) -> list`` fetch fn the More page uses."""
        self.more_button.set_visible(True)
        self.more_function = function

    def set_year_function(self, function: Callable) -> None:
        """Make the More page a year-filtered list driven by ``function``.

        ``function`` is ``fn(year, offset, limit) -> list``. The More page
        renders a year-filter DropDown ("All years" + album_years()) whose
        selection resets the auto-load with the year-narrowed fetch.
        """
        self.more_button.set_visible(True)
        self.year_function = function

    def enable_refresh(self, enabled: bool = True) -> None:
        """Opt-in: show the header refresh button (hidden by default).

        Only sections that support a force-rebuild (the Custom mixes carousel)
        call this. The button emits ``refresh-clicked``; the page wires that
        signal and runs the rebuild off the main thread.
        """
        self.refresh_button.set_visible(enabled)

    def set_refreshing(self, refreshing: bool) -> None:
        """Reflect an in-flight refresh: insensitive button + spinner face.

        While refreshing the button is desensitised and its icon is swapped for
        an ``Adw.Spinner`` so the user sees the rebuild is running; when it
        clears, the ``view-refresh-symbolic`` icon is restored and the button is
        re-enabled. Safe to call repeatedly.
        """
        self.refresh_button.set_sensitive(not refreshing)
        if refreshing:
            spinner = Adw.Spinner()
            self.refresh_button.set_child(spinner)
        else:
            # set_child(None) restores the implicit icon-name face.
            self.refresh_button.set_child(None)
            self.refresh_button.set_icon_name("view-refresh-symbolic")

    def _on_refresh_clicked(self, *args):
        self.emit("refresh-clicked")

    def set_items(self, items_list) -> None:
        """Populate the carousel with cards for ``items_list``."""
        self.items = list(items_list)

        cards_added = 0
        for index, item in enumerate(self.items):
            if index >= _INLINE_CARDS:
                self.more_button.set_visible(True)
                break
            card = HTCardWidget(item)
            self.disconnectables.append(card)
            self.cards_box.append(card)
            cards_added += 1

        if cards_added > 1:
            self.next_button.set_sensitive(True)

        GLib.idle_add(self._update_button_sensitivity)

    def set_card_widgets(self, widgets) -> None:
        """Populate with pre-built card widgets (e.g. AI collage cards).

        Same header/arrow chrome as ``set_items``, but the caller constructs
        (and owns the activation handlers of) the cards; each is registered in
        ``self.disconnectables`` so ``disconnect_all`` tears it down. The More
        button stays hidden — these sections show all their cards inline.
        """
        count = 0
        for card in widgets:
            self.disconnectables.append(card)
            self.cards_box.append(card)
            count += 1
        if count > 1:
            self.next_button.set_sensitive(True)
        GLib.idle_add(self._update_button_sensitivity)

    def _update_button_sensitivity(self, *args):
        adjustment = self.carousel_scrolled_window.get_hadjustment()
        if not adjustment:
            return
        value = adjustment.get_value()
        upper = adjustment.get_upper()
        page_size = adjustment.get_page_size()
        max_scroll = upper - page_size

        self.prev_button.set_sensitive(value > 0)
        self.next_button.set_sensitive(value < max_scroll)

    def on_more_clicked(self, *args):
        """Push a from-function page showing the full list."""
        from ..pages import HTFromFunctionPage

        if self.year_function is not None:
            page = HTFromFunctionPage(
                self.title, item_type=self.item_type, year_filter=True
            )
            page.set_year_function(self.year_function)
        else:
            page = HTFromFunctionPage(self.title, item_type=self.item_type)
            if self.more_function is None:
                page.set_items(self.items)
            else:
                page.set_function(self.more_function)
        page.load()
        utils.navigation_view.push(page)

    def carousel_go_next(self, *args):
        adjustment = self.carousel_scrolled_window.get_hadjustment()
        if not adjustment:
            return
        page_size = adjustment.get_page_size()
        value = adjustment.get_value()
        upper = adjustment.get_upper()
        new_value = min(value + page_size, upper - page_size)
        self._animate_carousel(adjustment, value, new_value)

    def carousel_go_prev(self, *args):
        adjustment = self.carousel_scrolled_window.get_hadjustment()
        if not adjustment:
            return
        page_size = adjustment.get_page_size()
        value = adjustment.get_value()
        new_value = max(value - page_size, 0)
        self._animate_carousel(adjustment, value, new_value)

    def _animate_carousel(self, adjustment, from_value, to_value):
        """Animate the scroll with an Adwaita spring animation."""
        if from_value == to_value:
            return

        target = Adw.CallbackAnimationTarget.new(adjustment.set_value)
        spring_params = Adw.SpringParams.new(
            damping_ratio=1.0, mass=1.0, stiffness=1200.0
        )
        animation = Adw.SpringAnimation.new(
            self.carousel_scrolled_window,
            from_value,
            to_value,
            spring_params,
            target,
        )
        animation.set_clamp(True)
        animation.play()
