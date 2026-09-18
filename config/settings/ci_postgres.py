"""Test settings that run the suite against Postgres.

The default test settings use in-memory SQLite because it is fast and needs no
service. Production runs Postgres, and the raw SQL in
:mod:`nuvix.analytics.queries` is precisely the code most able to pass on one
and fail on the other -- date truncation, window functions and division
semantics all differ. CI therefore runs the whole suite twice.
"""

import dj_database_url

from config.settings.env import env_str
from config.settings.test import *

DATABASES = {
    "default": dj_database_url.parse(
        env_str("DATABASE_URL", "postgres://nuvix:nuvix@localhost:5432/nuvix_test")
    )
}
