SELECT
  t0."index",
  t0."Unnamed: 0",
  t0."id",
  t0."bool_col",
  t0."tinyint_col",
  t0."smallint_col",
  t0."int_col",
  t0."bigint_col",
  t0."float_col",
  t0."double_col",
  t0."date_string_col",
  t0."string_col",
  t0."timestamp_col",
  t0."year",
  t0."month"
FROM ibis_testing.ibis_testing."FUNCTIONAL_ALLTYPES" AS t0
WHERE
  REGEXP_LIKE(t0."string_col", '0')