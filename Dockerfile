FROM php:7.4-apache-bullseye

ENV DEBIAN_FRONTEND=noninteractive\n\n# Disposable development instance: PHP and Git share the developer UID.\nRUN sed -i 's/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=developer/; s/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=developer/' /etc/apache2/envvars

LABEL org.opencontainers.image.title="Warexo Dev Runtime" \
      org.opencontainers.image.description="Disposable remote development runtime for Warexo" \
      org.opencontainers.image.source="https://github.com/aggrosoft/warexo-dev-runtime"

# Debian 11 (Bullseye) is archived. PHP 7.4 is required by the legacy Warexo runtime.
RUN printf '%s\n' \
      'deb [check-valid-until=no] http://archive.debian.org/debian bullseye main' \
      'deb [check-valid-until=no] http://archive.debian.org/debian bullseye-updates main' \
      'deb [check-valid-until=no] http://archive.debian.org/debian-security bullseye-security main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/*

RUN apt-get -o Acquire::Check-Valid-Until=false update \
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
        openssh-client \
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

RUN mkdir -p /opt/warexo/bin /var/www/html \
    && useradd -m -s /bin/bash developer \
    && usermod -aG www-data developer \
    && git config --system --add safe.directory /var/www/html

COPY docker/apache.conf /etc/apache2/sites-available/000-default.conf
COPY docker/php.ini /usr/local/etc/php/conf.d/warexo.ini
COPY docker/entrypoint.sh /usr/local/bin/warexo-entrypoint
COPY bin/ /opt/warexo/bin/

RUN chmod +x /usr/local/bin/warexo-entrypoint /opt/warexo/bin/*

WORKDIR /var/www/html

ENTRYPOINT ["/usr/local/bin/warexo-entrypoint"]
CMD ["apache2-foreground"]
