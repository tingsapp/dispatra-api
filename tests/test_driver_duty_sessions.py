"""Browser authentication must not silently change operational availability."""
from datetime import datetime, timezone
from conftest import login, post
from test_manual_operations import setup, check, BASE  # noqa: F401


def test_browser_login_logout_preserves_saved_duty_and_revokes_session(setup):
    w = setup
    browser = w['mobile']
    saved = check(browser.get(BASE + '/driver/profile'))['duty']
    credentials = ('driver', 'acme', w['driver']['email'], w['driver']['initial_password'])
    # Re-authentication and profile reads do not replace or close the duty session.
    login(browser, *credentials)
    assert check(browser.get(BASE + '/driver/profile'))['duty'] == saved
    assert browser.post('/api/v1/auth/logout').status_code == 204
    assert browser.get('/api/v1/auth/me').status_code == 401
    assert check(w['client'].get(BASE + '/drivers/' + w['driver']['id']))['on_duty'] is True
    login(browser, *credentials)
    assert check(browser.get(BASE + '/driver/profile'))['duty'] == saved
    # Explicit Off Duty still persists across logout and later login.
    check(post(browser, BASE + '/driver/duty/' + saved['id'] + '/end',
        {'version': saved['version'], 'ended_at': datetime.now(timezone.utc).isoformat()}))
    assert browser.post('/api/v1/auth/logout').status_code == 204
    login(browser, *credentials)
    assert check(browser.get(BASE + '/driver/profile'))['duty'] is None
    assert check(w['client'].get(BASE + '/drivers/' + w['driver']['id']))['on_duty'] is False
