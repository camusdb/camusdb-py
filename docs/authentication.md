# Authentication

Authentication is off on the server by default. Then you need no credentials.

When the server has authentication on, give a user and a password:

```python
conn = camusdb.connect(
    endpoint="https://db.example:5095", database="shop", user="app", password=secret
)
```

The driver logs in one time and gets a token. Each statement then sends the token, not the password.

- All the connections of the process with the same user and password share one token. This matters,
  because the server permits only 20 logins for each user each minute.
- The driver gets a new token before the old one expires. At 80% of the token lifetime, the driver
  continues to send the old token, and it logs in again in the background. A statement waits for a
  login only when there is no token, or when the token reached 95% of its lifetime.
- If a background login fails, the driver tries again 5 seconds later at the earliest.
- If the server refuses a login because it is busy (`CADB0518`), the driver sends the login again, up
  to three times, after a short random wait.
- If the server rejects the token, the driver logs in again and sends the statement one more time.

## A token from elsewhere

If you already have a token, give it with `access_token`. The driver does not renew this token.

## Log in later

To keep the password out of the settings, log in on the open connection:

```python
conn = camusdb.connect(endpoint="https://db.example:5095", database="shop")
conn.login("app", password)
...
conn.logout()
```

## Users and privileges

Users and privileges are SQL statements. Give the password as a parameter, so that it does not get
into the query logs:

```python
conn.execute("CREATE USER app IDENTIFIED BY @password", {"password": "app-secret"})
conn.execute("GRANT SELECT ON shop.* TO app")
```

## TLS

The driver sends credentials only over `https://`, or to this machine (`localhost`). For any other
endpoint, `connect()` raises an `InterfaceError` before it sends anything.

If TLS ends at a proxy that you trust, set `allow_insecure_credentials=True`.

## Error codes

| Code | Meaning |
| --- | --- |
| `CADB0516` | The login failed, or the token is not valid. |
| `CADB0517` | The user does not have the privilege. |
| `CADB0518` | The server is busy, or there are too many logins for that user. The driver tries the login again three times. Then wait one minute. |
| `CADB0519` | Credentials over plain HTTP. See [TLS](#tls). |
