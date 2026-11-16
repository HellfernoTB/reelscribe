# reelscribe migrations

`*.sql` files here are applied in name order by `reelscribe.state.apply_migrations` (recorded in
`schema_migrations`) to the connection's current schema: `instagram_recap` in production.

**Another project reads these tables.** daily-recap's view `daily_recap.reel_v` (daily-recap
`src/recap/migrations/versions_pg/pg_0002_reel_enrichment.py`) selects from `reel` (`id`,
`shortcode`, `url`, `uploader`, `title`, `caption`, `language`, `status`, `completed_at`,
`added_at`) and `transcript` (`reel_id`, `text`, `transcribed_at`). Its rows are also referenced by
`reel.id` (daily-recap `chunk.ref_id`, `reel_recap.reel_ids`).

- A migration that drops, renames or retypes one of those columns fails at startup
  (`cannot drop/alter ... because other objects depend on it`), and reelscribe-serve will not start.
  Coordinate with daily-recap: drop or change its view in a daily-recap migration first, then
  apply the reelscribe change, then recreate the view.
- Never renumber `reel.id` (no DELETE + re-insert of existing reels); `scripts/import_to_pg.py
  --replace` refuses for that reason.
- Adding tables or nullable columns is safe.
