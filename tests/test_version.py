from importlib.metadata import version

import camusdb


def test_the_module_version_matches_the_package_metadata():
    # The release workflow checks the tag against pyproject.toml; this keeps __version__ in step.
    assert camusdb.__version__ == version("camusdb-py")
