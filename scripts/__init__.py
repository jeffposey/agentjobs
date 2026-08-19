"""Repository tooling, a package so its modules have exactly one name.

Nothing imports this package for its contents; the scripts are run directly. It
exists because `scripts/build_release.py` imports `scripts.build_frontend`, and
without an `__init__.py` mypy resolves that same file under two module names --
`build_frontend` from walking the tree, `scripts.build_frontend` from the import --
and refuses to check *anything* in the repository until the ambiguity is gone:

    scripts\build_frontend.py: error: Source file found twice under different
    module names: "build_frontend" and "scripts.build_frontend"

One `__init__.py` makes `scripts.build_frontend` the only name.
"""
