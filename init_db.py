#!/usr/bin/env python3
"""init_db.py — 幂等建库（WAL）"""
import models

if __name__ == "__main__":
    mode = models.init_db()
    print(f"OK | db={models.DB_PATH} | journal_mode={mode}")
