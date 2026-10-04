"""Root pytest configuration.

Exists to register the project's own fixture plugins. ``pytest_plugins`` is only honoured in the
rootdir conftest, so this is where the catalogue fixture module is attached; the rest of the shared
fixtures live in ``tests/conftest.py`` next to the tests that use them.
"""

pytest_plugins = ["tests.catalog_fixtures"]
