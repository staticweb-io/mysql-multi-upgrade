-- The same on every version for the same data
SET NAMES utf8mb4;
SELECT 'posts', COUNT(*), SUM(CRC32(CONCAT_WS('|', id, title, body, IFNULL(meta, 'null'), DATE_FORMAT(created, '%Y-%m-%d %H:%i:%s')))) FROM app.posts;
SELECT 'legacy', COUNT(*), SUM(CRC32(CONCAT_WS('|', id, v))) FROM app.legacy;
SELECT 'view', COUNT(*) FROM app.post_titles;
SELECT 'fulltext', COUNT(*) FROM app.posts WHERE MATCH (body) AGAINST ('quoted');
CALL app.post_count(@n);
SELECT 'proc', @n;
