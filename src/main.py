# main.py
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

import signal
import sys
import logging
from gettext import gettext as _
from typing import Any, Callable, List

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .lib import utils
from .lib.player_object import AudioSink
from .window import TimbreWindow

logger = logging.getLogger(__name__)


class TimbreApplication(Adw.Application):
    """Main application singleton for Timbre."""

    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.tylerreece.timbre",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.create_action("quit", lambda *_: self.quit(), ["<primary>q", "<primary>w"])
        self.create_action("about", self.on_about_action)
        self.create_action("preferences", self.on_preferences_action, ["<primary>comma"])
        self.create_action(
            "shortcuts", self.on_shortcuts_action, ["<primary>question", "F1"]
        )
        # F5 refreshes the Home page (incremental sync + reload). The action
        # itself lives on the window (win.refresh-home); we only bind the accel.
        self.set_accels_for_action("win.refresh-home", ["F5"])
        # Transport + search shortcuts (actions live on the window).
        #
        # NB: Space is deliberately NOT bound here. A global accel installed via
        # set_accels_for_action fires at the CAPTURE phase, ahead of the focused
        # widget — so typing a space into a Gtk.SearchEntry/Gtk.Text (e.g. the
        # Explore search box) would toggle play/pause instead of inserting a
        # space. The window installs a BUBBLE-phase Gtk.ShortcutController for
        # Space instead, so a focused editable consumes the key first and the
        # play-pause shortcut only fires when nothing editable handled it.
        self.set_accels_for_action("win.next-track", ["<primary>Right"])
        self.set_accels_for_action("win.prev-track", ["<primary>Left"])
        self.set_accels_for_action("win.focus-search", ["<primary>f"])

        utils.init()
        utils.setup_logging()

        # Log display type for Wayland verification (Step 0.5)
        display = Gdk.Display.get_default()
        if display:
            logger.info(f"GDK display type: {display.__class__.__name__}")

        self.settings: Gio.Settings = Gio.Settings.new("io.github.tylerreece.timbre")
        self.alsa_devices = utils.get_alsa_devices()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Route POSIX termination signals through quit() so do_shutdown runs
        # (and the player is torn down cleanly). The .in launcher resets SIGINT
        # to SIG_DFL, so without this SIGTERM/SIGINT would kill the process
        # before do_shutdown could flush the reporter / NULL the pipeline.
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_unix_signal
        )
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_unix_signal
        )

    def _on_unix_signal(self) -> bool:
        logger.info("Received termination signal; quitting")
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self) -> None:
        self.win: TimbreWindow | None = self.props.active_window
        if not self.win:
            self.win = TimbreWindow(application=self)
        self.win.present()

    def do_shutdown(self) -> None:
        """Application teardown (canonical Gtk.Application hook).

        Drive the player's clean shutdown (final report_stop + reporter
        flush/close + pipeline → NULL) before chaining up. We must chain up to
        ``Adw.Application.do_shutdown`` or GTK will warn / leak.
        """
        win = self.props.active_window or getattr(self, "win", None)
        player = getattr(win, "player_object", None) if win is not None else None
        if player is not None:
            try:
                player.shutdown()
            except Exception:
                logger.exception("Error during player shutdown")
        db = getattr(win, "db", None) if win is not None else None
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.exception("Error closing database")
        Adw.Application.do_shutdown(self)

    def on_about_action(self, widget: Any, *args) -> None:
        about = Adw.AboutDialog(
            application_name="Timbre",
            application_icon="io.github.tylerreece.timbre",
            developer_name="The Timbre Contributors",
            version="0.1.0",
            developers=["Tyler Reece https://github.com/tylerreece"],
            license_type="GTK_LICENSE_GPL_3_0",
            issue_url="https://github.com/tylerreece/timbre/issues",
            website="https://github.com/tylerreece/timbre",
        )
        # Attribution to upstream project
        about.add_acknowledgement_section(
            _("Based on High Tide by Nokse22"),
            ["Nokse https://github.com/Nokse22"],
        )
        about.present(self.props.active_window)

    def on_shortcuts_action(self, *args) -> None:
        """Show the keyboard shortcuts dialog (Ctrl+? / F1).

        The dialog is defined in ``data/shortcuts-dialog.blp`` and compiled into
        the gresource bundle; we load it through a builder and present it on the
        active window. Falls back silently if the resource is unavailable (e.g.
        running before the bundle is built).
        """
        try:
            builder = Gtk.Builder.new_from_resource(
                "/io/github/tylerreece/timbre/shortcuts-dialog.ui"
            )
            dialog = builder.get_object("shortcuts_dialog")
        except Exception:
            logger.exception("Could not load shortcuts dialog")
            return
        if dialog is None:
            return
        dialog.present(self.props.active_window)

    def on_preferences_action(self, *args) -> None:
        win = self.props.active_window
        if win is None:
            return
        from .preferences import JTPreferences

        dialog = JTPreferences(
            window=win,
            secret_store=win.secret_store,
            alsa_devices=self.alsa_devices,
            on_logout=win.logout,
        )
        dialog.present(win)

    def create_action(
        self, name: str, callback: Callable, shortcuts: List[str] | None = None
    ) -> None:
        action: Gio.SimpleAction = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)


def main(version: str) -> int:
    """The application's entry point."""
    app: TimbreApplication = TimbreApplication()
    return app.run(sys.argv)
