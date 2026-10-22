from .conftest import PART, login, new_request, last_request_id


def test_smoke(client, users, stock, sql):
    assert client.get('/').status_code == 302
    assert login(client, 'analyst1').status_code == 302
    r = new_request(client)
    assert r.status_code == 302, r.get_data(as_text=True)
    rid = last_request_id(sql)
    for url in ['/', '/requests/', '/lookup/?material=' + PART, '/requests/?status=reviewed',
                '/requests/?status=cancelled', '/api/dashboard.json']:
        r = client.get(url); assert r.status_code == 200, (url, r.get_data(as_text=True)[:2000])
    client.post('/logout/')
    login(client, 'counter1')
    r = client.post(f'/requests/{rid}/claim/'); assert r.status_code == 302
    r = client.get(f'/counts/{rid}/'); assert r.status_code == 200, r.get_data(as_text=True)[:3000]
    r = client.get(f'/counts/{rid}/explosion/'); assert r.status_code == 200, r.get_data(as_text=True)[:3000]
    client.post('/logout/')
    login(client, 'admin1')
    for url in ['/admin/users/', '/admin/users/new/', f'/counts/{rid}/', '/']:
        r = client.get(url); assert r.status_code == 200, (url, r.get_data(as_text=True)[:3000])
