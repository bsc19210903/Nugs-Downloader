# OpenMediaGraph next-five read-only research harness

This directory is an isolated research harness. It does not add a product runtime,
production acquisition path, database authority, persistent service, or storage
migration to Nugs-Downloader.

The harness executes five bounded studies:

1. checksummed acquisition and manifesting of OpenSLR YesNo and the CC BY ESC-10
   subset pinned to an upstream commit;
2. a read-only probe of a public Hugging Face Xet-backed file and its local cache;
3. an exact shadow-loader comparison with an explicit accelerator-availability
   gate;
4. a real local multiprocess source-affinity canary plus adversarial node-loss,
   cache-drift, and stampede simulations;
5. a local SQLite WAL transactional metadata prototype with publication, snapshot,
   legal-hold, garbage-collection, crash, corruption, and repair campaigns.

Source media is downloaded into the ephemeral runner directory and is never
included in the uploaded research artifact. The artifact contains only aggregate
measurements, hashes, manifests, package versions, and evidence boundaries.

The workflow is research-only and read-only with respect to external systems. It
uses no credentials, uploads no data, creates no remote datasets, and cannot
mutate Nugs-Downloader, Archive.org, Hugging Face, or the upstream source
repositories.
