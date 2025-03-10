SELECT
  t0.x IN (
    SELECT
      t1.x
    FROM (
      SELECT
        t0.x AS x
      FROM t AS t0
      WHERE
        t0.x > CAST(2 AS SMALLINT)
    ) AS t1
  ) AS "Contains(x, x)"
FROM t AS t0