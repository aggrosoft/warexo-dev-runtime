FROM php:7.4-apache

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-server \
        unzip \
        zip \
        zstd \
        rsync \
        default-mysql-client \
        libicu-dev \
        libzip-dev \
        libpng-dev \
        libjpeg62-turbo-dev \
        libfreetype6-dev \
        libxml2-dev \
        libcurl4-openssl-dev \
        libonig-dev \
        libkrb5-dev \
        libc-client2007e-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-configure imap --with-kerberos --with-imap-ssl \
    && docker-php-ext-install -j"$(nproc)" \
        bcmath \
        curl \
        gd \
        imap \
        intl \
        mbstring \
        mysqli \
        pdo_mysql \
        soap \
        zip \
    && a2enmod rewrite headers \
    && rm -rf /var/lib/apt/lists/*

RUN php -r "copy('https://getcomposer.org/installer', '/tmp/composer-setup.php');" \
    && php /tmp/composer-setup.php --1 --install-dir=/usr/local/bin --filename=composer \
    && rm /tmp/composer-setup.php

RUN mkdir -p /run/sshd /opt/warexo/bin /var/www/html \
    && useradd -m -s /bin/bash developer \
    && usermod -aG www-data developer

COPY docker/apache.conf /etc/apache2/sites-available/000-default.conf
COPY docker/php.ini /usr/local/etc/php/conf.d/warexo.ini
COPY docker/sshd_config /etc/ssh/sshd_config
COPY docker/entrypoint.sh /usr/local/bin/warexo-entrypoint
COPY bin/ /opt/warexo/bin/

RUN chmod +x /usr/local/bin/warexo-entrypoint /opt/warexo/bin/*

WORKDIR /var/www/html

ENTRYPOINT ["/usr/local/bin/warexo-entrypoint"]
CMD ["apache2-foreground"]
