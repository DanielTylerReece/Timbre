# i18n / gettext pipeline for Timbre.
#
# The 'glib' preset wires xgettext with the GLib/GTK keyword set
# (-k_ -kN_ -kC_:1c,2 -kNC_:1c,2 -kg_dgettext:2 -kg_dngettext:2,3 ...),
# which is exactly what extracts both the python `_("...")` calls and the
# blueprint `.blp` `_("...")` / `C_("ctx", "...")` calls (blueprint's string
# syntax is C-like, so the C lexer reads .blp sources directly — they are
# listed verbatim in POTFILES.in, no generated .ui indirection needed).
#
# Targets this provides:
#   ninja -C build timbre-pot      # regenerate po/timbre.pot from POTFILES.in
#   ninja -C build timbre-update-po # merge the pot into every <lang>.po
#   ninja -C build/install ...      # .mo files compiled + installed per LINGUAS
#
# The .pot is intentionally NOT committed (GNOME convention): it is a generated
# artifact, regenerated on demand by the `timbre-pot` target. Translators run
# that target (or `msginit`) to seed a new <lang>.po, add the code to LINGUAS,
# and the build compiles/installs <lang>.mo to <prefix>/share/locale/<lang>/
# LC_MESSAGES/timbre.mo — where the launcher's bindtextdomain('timbre', localedir)
# resolves it at runtime.

i18n = import('i18n')

i18n.gettext('timbre',
  preset: 'glib',
  args: [
    '--from-code=UTF-8',
    '--add-comments',
    '--package-name=timbre',
    '--package-version=' + meson.project_version(),
    '--msgid-bugs-address=https://github.com/DanielTylerReece/Timbre/issues',
  ],
)
