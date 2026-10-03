# Retired Superbot experiments

These Bocha advertising classifiers are historical experiments, not production
plugin modules. No plugin lifecycle, player command or management callback imports
them. Do not load credentials, launch workers or enable them during maintenance.
Production moderation uses existing rules and approved local fingerprints.

The archived engine remains covered by mocked offline regression tests to preserve
experimental evidence. Historical AI database tables and moderation audits remain
in place; moving source code does not delete or replay their records.
