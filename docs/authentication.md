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
- The driver gets a new token before the old one expires.
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
| `CADB0518` | Too many logins for that user. Wait one minute. |
| `CADB0519` | Credentials over plain HTTP. See [TLS](#tls). |
