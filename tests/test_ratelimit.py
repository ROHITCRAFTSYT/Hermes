from ratelimit import RateLimiter


def test_window_slides():
    now = [0.0]
    rl = RateLimiter(2, window_s=60, clock=lambda: now[0])
    assert rl.check("a") is None and rl.check("a") is None
    assert rl.check("a") == 60
    assert rl.check("b") is None          # per client
    now[0] = 30
    assert rl.check("a") == 30
    now[0] = 60.5
    assert rl.check("a") is None


def test_zero_disables():
    rl = RateLimiter(0)
    assert all(rl.check("a") is None for _ in range(100))
