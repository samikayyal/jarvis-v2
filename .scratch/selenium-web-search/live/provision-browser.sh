set -eu
stage=/home/samik/Projects/Jarvis/code/web-browser-provision
mkdir -p "$stage"
cd "$stage"
curl --fail --location --retry 2 --max-time 180 -o google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo -n apt-get install -y ./google-chrome.deb
version=$(/usr/bin/google-chrome-stable --version | awk '{print $3}')
curl --fail --location --retry 2 --max-time 180 -o chromedriver.zip "https://storage.googleapis.com/chrome-for-testing-public/$version/linux64/chromedriver-linux64.zip"
unzip -o chromedriver.zip
sudo -n install -m 0755 chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
/usr/bin/google-chrome-stable --version
/usr/local/bin/chromedriver --version
sha256sum google-chrome.deb chromedriver.zip
