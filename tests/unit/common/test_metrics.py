from trading.common.metrics import REGISTRY, _Registry


def test_counter_increments_and_renders():
    r = _Registry()
    c = r.counter("tea_test_counter", "test help", {"stream": "x"})
    c.inc()
    c.inc(3)
    out = r.render()
    assert "tea_test_counter" in out
    assert 'stream="x"' in out
    assert 'tea_test_counter{stream="x"} 4' in out.replace("'", '"')


def test_gauge_set():
    r = _Registry()
    g = r.gauge("tea_test_gauge", "gauge help", {"adapter": "binance"})
    g.set(12.5)
    out = r.render()
    assert "tea_test_gauge" in out
    assert "12.5" in out


def test_module_registry_exists():
    assert isinstance(REGISTRY, _Registry)
