# i18n deferred to post-1.0; desktop/appdata files installed directly without po merge
desktop_file = configure_file(
        input: 'io.github.tylerreece.timbre.desktop.in',
       output: 'io.github.tylerreece.timbre.desktop',
  copy: true,
)
install_data(
  desktop_file,
  install_dir: join_paths(get_option('datadir'), 'applications')
)

blueprints = custom_target('blueprints',
  input: files(
    'ui/pages_ui/page_template.blp',
    'ui/pages_ui/tracks_list_template.blp',
    'ui/pages_ui/mix_page_template.blp',
    'ui/pages_ui/artist_page_template.blp',
    'ui/widgets/card_widget.blp',
    'ui/widgets/wide_card_widget.blp',
    'ui/widgets/collage_card_widget.blp',
    'ui/widgets/carousel_widget.blp',
    'ui/widgets/queue_widget.blp',
    'ui/widgets/queue_row.blp',
    'ui/widgets/generic_track_widget.blp',
    'ui/widgets/lyrics_widget.blp',
    'ui/widgets/auto_load_widget.blp',
    'ui/window.blp',
    'ui/onboarding.blp',
    'ui/new_playlist.blp',
    'ui/preferences.blp',
    'ui/detailed_track_listing.blp',
    'ui/card_template.blp',
    'shortcuts-dialog.blp',
  ),
  output: '.',
  command: [find_program('blueprint-compiler'), 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

gnome.compile_resources('timbre',
  'timbre.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

appstream_file = configure_file(
        input: 'io.github.tylerreece.timbre.appdata.xml.in',
       output: 'io.github.tylerreece.timbre.appdata.xml',
  copy: true,
)
install_data(
  appstream_file,
  install_dir: join_paths(get_option('datadir'), 'metainfo')
)

appstream_util = find_program('appstream-util', required: false)
if appstream_util.found()
  test('Validate appstream file', appstream_util, args: ['validate', appstream_file])
endif

install_data('io.github.tylerreece.timbre.gschema.xml',
  install_dir: join_paths(get_option('datadir'), 'glib-2.0/schemas')
)

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('Validate schema file',
       compile_schemas,
       args: ['--strict', '--dry-run', meson.current_source_dir()])
endif

subdir('icons')
