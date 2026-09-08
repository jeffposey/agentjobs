-- AgentJobs authoritative SQLite store, physical schema version 2.
--
-- analytics-design section 6, item C: `project.reporting_tz` holds an IANA zone name
-- such as 'America/Chicago', never a fixed offset. Section 3.5 measured why -- SQLite
-- ships no timezone database, its date() modifier takes a *fixed* offset, and Central
-- time is -06:00 in winter and -05:00 in summer, so either constant misfiles
-- late-evening work by a day for half of every year.
--
-- 001 stated that rule in a comment. This states it as a constraint, which is what the
-- rest of this schema does with its invariants: `task`'s consistency rules are CHECKs
-- rather than a Python validator, and the queue slot is a unique index rather than a
-- scan. `agentjobs.sqlstore.reporting_tz` is the same rule at the one door that writes
-- the column today and gives a fuller message; this catches the writer that does not
-- come through it -- an operator at a sqlite3 prompt, or a future setter.
--
-- Triggers rather than a CHECK because SQLite cannot add a CHECK to an existing table,
-- and rebuilding a table that every other table references by foreign key is a large
-- risk to take for a small rule.
--
-- What is checked here is *shape*: an offset is refused, and so is anything that
-- cannot be a zone name. Whether the name is a zone this machine knows needs a
-- timezone database, which SQL has no access to, so that half stays in Python.

CREATE TRIGGER project_reporting_tz_is_a_zone_name_on_insert
BEFORE INSERT ON project
FOR EACH ROW WHEN
     trim(NEW.reporting_tz) = ''
  OR NEW.reporting_tz GLOB '[+-]*'
  OR NEW.reporting_tz NOT GLOB '[A-Za-z]*'
  OR NEW.reporting_tz GLOB '*[^A-Za-z0-9_+/-]*'
BEGIN
  SELECT RAISE(ABORT, 'reporting_tz must be an IANA zone name such as America/Chicago, not a fixed offset: an offset is correct for half the year (analytics-design section 3.5)');
END;

CREATE TRIGGER project_reporting_tz_is_a_zone_name_on_update
BEFORE UPDATE OF reporting_tz ON project
FOR EACH ROW WHEN
     trim(NEW.reporting_tz) = ''
  OR NEW.reporting_tz GLOB '[+-]*'
  OR NEW.reporting_tz NOT GLOB '[A-Za-z]*'
  OR NEW.reporting_tz GLOB '*[^A-Za-z0-9_+/-]*'
BEGIN
  SELECT RAISE(ABORT, 'reporting_tz must be an IANA zone name such as America/Chicago, not a fixed offset: an offset is correct for half the year (analytics-design section 3.5)');
END;
