CREATE TABLE users (id INT PRIMARY KEY, username VARCHAR(100) NOT NULL, enabled BOOLEAN NOT NULL,
 roles TEXT NOT NULL, password VARCHAR(255) NOT NULL, salt VARCHAR(255) NOT NULL,
 locked BOOLEAN NOT NULL DEFAULT 0, expired BOOLEAN NOT NULL DEFAULT 0,
 credentials_expired BOOLEAN NOT NULL DEFAULT 0, email VARCHAR(255)) ENGINE=InnoDB;
INSERT INTO users VALUES
 (1,'fixture-admin',1,'a:1:{i:0;s:16:"ROLE_SUPER_ADMIN";}','source-password','source-salt',1,1,1,'admin@source.invalid'),
 (2,'fixture-user',1,'a:0:{}','source-password-two','source-salt-two',0,0,0,'user@source.invalid');
CREATE TABLE client (id INT PRIMARY KEY, shopUser VARCHAR(100), shopPassword VARCHAR(100),
 smtpHost VARCHAR(100), smtpUserName VARCHAR(100), smtpPassword VARCHAR(100), email VARCHAR(255)) ENGINE=InnoDB;
INSERT INTO client VALUES (1,'source-shop-user','source-shop-password','source-mail.invalid','smtp-user','smtp-password','client@source.invalid');
CREATE TABLE customer (id INT PRIMARY KEY, email VARCHAR(255)) ENGINE=InnoDB;
INSERT INTO customer VALUES (1,'customer@source.invalid');
CREATE TABLE clone_fixture (id INT PRIMARY KEY, value VARCHAR(100) NOT NULL) ENGINE=InnoDB;
INSERT INTO clone_fixture VALUES (1,'unchanged business value');
INSERT INTO clone_fixture VALUES (2,'DEFINER=`data-user`@`data-host`');
-- This source-only account deliberately does not exist in the clone.
CREATE USER 'fixture_definer'@'localhost' IDENTIFIED BY 'synthetic-definer-password';
GRANT SELECT, UPDATE, TRIGGER, EVENT ON warexo_source.* TO 'fixture_definer'@'localhost';
CREATE DEFINER=`fixture_definer`@`localhost` TRIGGER fixture_update
 BEFORE UPDATE ON clone_fixture FOR EACH ROW SET NEW.value=NEW.value;
CREATE DEFINER=`fixture_definer`@`localhost` PROCEDURE fixture_definer_probe()
 SELECT 'first line
CREATE DEFINER=`body-user`@`body-host` VIEW unchanged
last line' AS marker;
CREATE DEFINER=`fixture_definer`@`localhost` EVENT fixture_disabled_event
 ON SCHEDULE EVERY 1 DAY DISABLE DO UPDATE clone_fixture SET value=value;
