# Maintainer: oreo1298
#
# Arch Linux and Arch-based distributions (CachyOS, EndeavourOS, Manjaro, Garuda, ...).
# Builds the working tree this file sits in:
#
#   git clone https://github.com/oreo1298/AquasuiteLinux.git
#   cd AquasuiteLinux
#   makepkg -si
#
# To update later: `git pull` then `makepkg -sif`.

pkgname=aquasuitelinux
pkgver=1.0.4
pkgrel=1
pkgdesc="Monitoring and fan control for Aquacomputer devices: fan curves, Delta T, alarms, aquasuite import"
arch=('any')
url="https://github.com/oreo1298/AquasuiteLinux"
license=('MIT')
depends=('python' 'pyside6' 'qt6-svg')
optdepends=('polkit: enable the background service from the app'
            'nvidia-utils: NVIDIA GPU temperatures as sensors'
            'libnotify: notify-send for alarm commands')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
install=aquasuitelinux.install

_srcdir="$startdir"

build() {
  cd "$_srcdir"
  rm -rf dist build
  python -m build --wheel --no-isolation
}

check() {
  cd "$_srcdir"
  QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
}

package() {
  cd "$_srcdir"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 packaging/udev/70-aquasuitelinux.rules "$pkgdir/usr/lib/udev/rules.d/70-aquasuitelinux.rules"
  install -Dm644 packaging/systemd/aquasuited.service "$pkgdir/usr/lib/systemd/system/aquasuited.service"
  install -Dm644 packaging/sysusers/aquasuitelinux.conf "$pkgdir/usr/lib/sysusers.d/aquasuitelinux.conf"
  install -Dm644 data/io.github.oreo1298.AquasuiteLinux.desktop \
    "$pkgdir/usr/share/applications/io.github.oreo1298.AquasuiteLinux.desktop"
  install -Dm644 data/io.github.oreo1298.AquasuiteLinux.metainfo.xml \
    "$pkgdir/usr/share/metainfo/io.github.oreo1298.AquasuiteLinux.metainfo.xml"
  install -Dm644 aquasuitelinux/data/aquasuitelinux.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.oreo1298.AquasuiteLinux.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 NOTICE "$pkgdir/usr/share/licenses/$pkgname/NOTICE"
  install -Dm644 README.md docs/PROTOCOL.md -t "$pkgdir/usr/share/doc/$pkgname/"
}
