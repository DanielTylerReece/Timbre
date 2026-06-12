moduledir = join_paths(pkgdatadir, 'timbre')
gnome = import('gnome')

python = import('python')

# Module objects — tidalapi/pypresence not required; Jellyfin layer added in Phase 1
py_installation = python.find_installation('python3', modules: ['gi.repository'])
if not py_installation.found()
    error('No valid python3 binary found')
endif


conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', join_paths(get_option('prefix'), get_option('localedir')))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'timbre.in',
  output: 'timbre',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x'
)

timbre_sources = [
  '__init__.py',
  'main.py',
  'window.py',
  'onboarding.py',
  'preferences.py',
  'ai_model_widgets.py',
  'mpris.py',
  'disconnectable_iface.py',
  'new_playlist.py',
]

install_data(timbre_sources, install_dir: moduledir)

# install_subdir('lib') recursively installs the Phase 1 Jellyfin subpackage
# (lib/jellyfin/: __init__.py, client.py, models.py) along with the rest of lib.
install_subdir('lib', install_dir: moduledir)
install_subdir('pages', install_dir: moduledir)
install_subdir('widgets', install_dir: moduledir)
