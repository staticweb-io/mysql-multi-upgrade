-- Runs on every supported version, so it sticks to features that
-- MariaDB 10.3 and MySQL 5.7 both have.
SET NAMES utf8mb4;
CREATE DATABASE app CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE app;

CREATE TABLE posts (
  id INT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(200) NOT NULL,
  body MEDIUMTEXT,
  meta JSON,
  created DATETIME NOT NULL,
  FULLTEXT KEY body_text (body)
) ENGINE=InnoDB;

INSERT INTO posts (title, body, meta, created) VALUES
  ('Hello', 'First post', '{"tags": ["a"]}', '2021-03-04 05:06:07'),
  ('Ünïcödé', 'Ünïcödé body 日本語 🎉', '{"tags": []}', '2021-06-07 08:09:10'),
  ('Quotes', 'It''s "quoted"', NULL, '2021-12-31 23:59:59');
-- 3 * 2^10 rows
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;
INSERT INTO posts (title, body, meta, created) SELECT CONCAT(title, '+'), body, meta, created + INTERVAL 1 DAY FROM posts;

CREATE TABLE legacy (id INT PRIMARY KEY, v VARCHAR(50)) ENGINE=MyISAM DEFAULT CHARSET=latin1;
INSERT INTO legacy VALUES (1, CONCAT('caf', CHAR(0xE9))), (2, 'two');

CREATE TABLE stats (name VARCHAR(20) PRIMARY KEY, n INT NOT NULL) ENGINE=InnoDB;
INSERT INTO stats VALUES ('inserts', 0);

CREATE TRIGGER count_inserts AFTER INSERT ON posts FOR EACH ROW
  UPDATE stats SET n = n + 1 WHERE name = 'inserts';

CREATE VIEW post_titles AS SELECT id, title FROM posts WHERE title LIKE 'H%';

DELIMITER //
CREATE PROCEDURE post_count(OUT n INT)
BEGIN
  SELECT COUNT(*) INTO n FROM posts;
END//
DELIMITER ;

CREATE USER 'app'@'localhost' IDENTIFIED BY 'app-password';
GRANT ALL PRIVILEGES ON app.* TO 'app'@'localhost';
