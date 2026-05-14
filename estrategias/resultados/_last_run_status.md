# último run VPS

Status: **FAIL**
Timestamp: 2026-05-14T06:00:05Z
Nota: todos los backtests fallaron

## stderr (últimas 20 líneas)

```
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/coder/Trading-Edge-App/.venv/lib/python3.12/site-packages/asyncpg/connect_utils.py", line 931, in __connect_addr
    tr, pr = await connector
             ^^^^^^^^^^^^^^^
  File "/home/coder/Trading-Edge-App/.venv/lib/python3.12/site-packages/asyncpg/connect_utils.py", line 802, in _create_ssl_connection
    tr, pr = await loop.create_connection(
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/asyncio/base_events.py", line 1122, in create_connection
    raise exceptions[0]
  File "/usr/lib/python3.12/asyncio/base_events.py", line 1104, in create_connection
    sock = await self._connect_sock(
           ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/asyncio/base_events.py", line 1007, in _connect_sock
    await self.sock_connect(sock, address)
  File "/usr/lib/python3.12/asyncio/selector_events.py", line 651, in sock_connect
    return await fut
           ^^^^^^^^^
  File "/usr/lib/python3.12/asyncio/selector_events.py", line 691, in _sock_connect_cb
    raise OSError(err, f'Connect call failed {address}')
ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5434)
```
