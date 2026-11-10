-- Runtime setup for the SAP ASE test server. Idempotent, and applied over TDS by
-- dev/ase/init.py, which tests/common.py and dev/perf/seed.py call.
-- The image already sets engines, memory, connections, locks and tempdb.

use master
go

-- Other databases make a column nullable unless it says NOT NULL. ASE does the opposite, which
-- the tests' CREATE TABLE statements don't expect.
-- 8192 is the status bit of 'allow nulls by default'
if not exists (select 1 from master..sysdatabases where name = 'testdb' and status & 8192 = 8192)
    exec sp_dboption testdb, 'allow nulls by default', true
go

use testdb
go
checkpoint
go

-- Hashing univarchar converts it one character at a time, which makes long queries, and parallel
-- test runs compile many of them at once. 120000 pages (234 MB) fits in the image's max memory
-- of 786432 pages, so the 2g container limit still holds. It's dynamic, but kept in the
-- container's ASE.cfg, so a recreated container needs this again.
use master
go
if (select value from master..sysconfigures where comment = 'procedure cache size') < 120000
    exec sp_configure 'procedure cache size', 120000
go

-- A login that can read but not write, for tests/test_sybase.py
use master
go
if not exists (select 1 from master..syslogins where name = 'dd_reader')
    exec sp_addlogin 'dd_reader', 'Reader#1@x/y', 'testdb'
go
use testdb
go
if not exists (select 1 from sysusers where name = 'dd_reader')
    exec sp_adduser 'dd_reader'
go
