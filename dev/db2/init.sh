#!/bin/bash
# Run by the Db2 image once, after it creates the database.
# The default transaction log fills up during inserts of about 1M rows.
su - db2inst1 -c "db2 update db cfg for testdb using LOGFILSIZ 16384 LOGPRIMARY 20 LOGSECOND 40"
su - db2inst1 -c "db2 force applications all"
su - db2inst1 -c "db2 deactivate db testdb"
su - db2inst1 -c "db2 activate db testdb"
