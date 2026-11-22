# Ideas for future work

These are ideas for consideration, not committed features.

## Recognition charts

Explore showing popular recognized recordings on the public recognition page.
The existing recognition counters contain only overall attempts and outcomes;
they cannot reconstruct past counts per recording.

Any design would need separately aggregated counts for successful matches,
without using private account history or storing audio, IP addresses or
individual recognition events. Decide how to group recording variants, handle
duplicate requests and prevent manipulation before publishing rankings. Define
retention, privacy, database migration and the impact of additional writes for
both SQLite and PostgreSQL before implementation.
