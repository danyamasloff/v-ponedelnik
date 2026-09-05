"""Publishing to messenger platforms.

The package is intentionally storage-free: nothing here writes to PostgreSQL.
Publication history (`publications` and friends) is a later phase; keeping the
adapter stateless lets the MAX integration be tested end to end without
inventing a schema that will be designed properly later.
"""
