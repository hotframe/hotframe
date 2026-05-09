def test_all_lazy_imports_resolve():
    import hotframe

    for name in hotframe.__all__:
        assert getattr(hotframe, name) is not None  # force the lazy import to resolve
