"""DIAL core: shared middleware, feature flags, audit log and developer tooling.

Management commands:

* ``seed_demo``  - create/reset the *Demo Camp* event with realistic data
* ``dial_token``  - mint a service-account API token
* ``dial_a11y``   - run the accessibility linter (``apps.core.a11y``) over the smoke-test pages
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    verbose_name = "DIAL Core"
