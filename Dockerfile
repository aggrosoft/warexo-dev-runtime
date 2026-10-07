FROM php:7.4-apache-bullseye

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        default-mysql-client \
        git \
        libcurl4-openssl-dev \
        libfreetype6-dev \
        libicu-dev \
        libjpeg62-turbo-dev \
        libonig-dev \
        libpng-dev \
        libxml2-dev \
        libzip-dev \
        openssh-server \
        rsync \
        sudo \
        unzip \
        zip \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN docker-php-ext-configure gd --with-freetype --with-jpeg

RUN docker-php-ext-install -j"$(nproc)" \
        bcmath \
        curl \
        gd \
        intl \
        mbstring \
        mysqli \
        pdo_mysql \
        soap \
        zip

RUN a2enmod rewrite headers

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
