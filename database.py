"""Async MySQL persistence for receipts and participant selections."""
import os

import aiomysql


pool = None


async def initialize():
    global pool
    pool = await aiomysql.create_pool(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "halfcheck"),
        password=os.getenv("MYSQL_PASSWORD", "halfcheck"),
        db=os.getenv("MYSQL_DATABASE", "halfcheck"),
        minsize=1,
        maxsize=10,
        autocommit=True,
        charset="utf8mb4",
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    id CHAR(32) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    created_at DATETIME(6) NOT NULL,
                    total_sum DECIMAL(12, 2) NOT NULL
                ) CHARACTER SET utf8mb4
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS receipt_items (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    receipt_id CHAR(32) NOT NULL,
                    item_index INT NOT NULL,
                    name VARCHAR(512) NOT NULL,
                    total_sum DECIMAL(12, 2) NOT NULL,
                    quantity DECIMAL(12, 3) NOT NULL,
                    UNIQUE KEY receipt_item_order (receipt_id, item_index),
                    CONSTRAINT fk_item_receipt FOREIGN KEY (receipt_id)
                        REFERENCES receipts(id) ON DELETE CASCADE
                ) CHARACTER SET utf8mb4
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS selections (
                    item_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    PRIMARY KEY (item_id, user_id),
                    CONSTRAINT fk_selection_item FOREIGN KEY (item_id)
                        REFERENCES receipt_items(id) ON DELETE CASCADE
                ) CHARACTER SET utf8mb4
            """)


async def close():
    if pool is not None:
        pool.close()
        await pool.wait_closed()


async def list_receipts():
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id, name, created_at, total_sum FROM receipts ORDER BY created_at DESC")
            rows = await cur.fetchall()
    return {row["id"]: row for row in rows}


async def get_receipt(receipt_id):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id, name, created_at, total_sum FROM receipts WHERE id=%s", (receipt_id,))
            receipt = await cur.fetchone()
            if not receipt:
                return None
            await cur.execute("""
                SELECT i.item_index AS item_index, i.name, i.total_sum, i.quantity,
                       COALESCE(GROUP_CONCAT(s.user_id), '') AS selected_ids
                FROM receipt_items i LEFT JOIN selections s ON s.item_id=i.id
                WHERE i.receipt_id=%s GROUP BY i.id ORDER BY i.item_index
            """, (receipt_id,))
            items = await cur.fetchall()
    receipt["items"] = [
        {"name": row["name"], "total_sum": float(row["total_sum"]),
         "quantity": float(row["quantity"]),
         "selected_by": {int(uid) for uid in row["selected_ids"].split(",") if uid}}
        for row in items
    ]
    receipt["total_sum"] = float(receipt["total_sum"])
    return receipt


async def create_receipt(receipt_id, name, created_at, total_sum, items):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute("INSERT INTO receipts (id,name,created_at,total_sum) VALUES (%s,%s,%s,%s)",
                                  (receipt_id, name, created_at, total_sum))
                for index, item in enumerate(items):
                    await cur.execute("""INSERT INTO receipt_items
                        (receipt_id,item_index,name,total_sum,quantity) VALUES (%s,%s,%s,%s,%s)""",
                                      (receipt_id, index, item["name"], item["total_sum"], item["quantity"]))
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


async def toggle_selection(receipt_id, item_index, user_id):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""SELECT i.id FROM receipt_items i JOIN receipts r ON r.id=i.receipt_id
                WHERE r.id=%s AND i.item_index=%s""", (receipt_id, item_index))
            row = await cur.fetchone()
            if not row:
                return False
            await cur.execute("DELETE FROM selections WHERE item_id=%s AND user_id=%s", (row[0], user_id))
            if cur.rowcount == 0:
                await cur.execute("INSERT INTO selections (item_id,user_id) VALUES (%s,%s)", (row[0], user_id))
                return True
            return False
