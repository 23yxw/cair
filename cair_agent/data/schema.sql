-- Case data as a relational schema. Portable to Postgres/MySQL (no SQLite-specific types).
-- The agent-facing repository only touches patients/studies/series/images/reports.
-- eval_labels holds ground truth and is read exclusively by the evaluator.

CREATE TABLE IF NOT EXISTS patients (
    case_id       TEXT PRIMARY KEY,
    age           TEXT,
    sex           TEXT,
    presentation  TEXT
);

CREATE TABLE IF NOT EXISTS studies (
    study_id      TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES patients(case_id),
    modality      TEXT NOT NULL,
    study_order   INTEGER NOT NULL,
    n_series      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_studies_case ON studies(case_id, study_order);

CREATE TABLE IF NOT EXISTS series (
    series_id        TEXT PRIMARY KEY,
    study_id         TEXT NOT NULL REFERENCES studies(study_id),
    caption          TEXT NOT NULL DEFAULT '',
    image_count      INTEGER NOT NULL,
    anchor_slice     INTEGER,
    anchor_image_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_series_study ON series(study_id);

CREATE TABLE IF NOT EXISTS images (
    image_id     TEXT PRIMARY KEY,
    series_id    TEXT NOT NULL REFERENCES series(series_id),
    study_id     TEXT NOT NULL REFERENCES studies(study_id),
    slice_index  INTEGER NOT NULL,
    uri          TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_images_series ON images(series_id, slice_index);

CREATE TABLE IF NOT EXISTS reports (
    study_id     TEXT NOT NULL REFERENCES studies(study_id),
    report_type  TEXT NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (study_id, report_type)
);

CREATE TABLE IF NOT EXISTS eval_labels (
    case_id     TEXT PRIMARY KEY REFERENCES patients(case_id),
    label       TEXT NOT NULL,
    discussion  TEXT NOT NULL
);
