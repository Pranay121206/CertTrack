
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

WEBSITE = (
    "file:///C:/Users/Pranay/OneDrive/Desktop/"
    "wt-fixed/wt-fixed/index.html"
)


def open_browser():
    options = Options()
    options.add_argument("--start-maximized")
    return webdriver.Chrome(options=options)


def test_page_title():
    driver = open_browser()
    try:
        driver.get(WEBSITE)

        print("Page title:", driver.title)

        assert driver.title.strip() != ""
    finally:
        driver.quit()


def test_page_has_visible_content():
    driver = open_browser()
    try:
        driver.get(WEBSITE)

        body = driver.find_element(By.TAG_NAME, "body")

        assert body.is_displayed()
        assert len(body.text.strip()) > 0

        print("Page content loaded successfully.")
    finally:
        driver.quit()


def test_page_has_images_and_buttons():
    driver = open_browser()
    try:
        driver.get(WEBSITE)

        images = driver.find_elements(By.TAG_NAME, "img")
        buttons = driver.find_elements(By.TAG_NAME, "button")

        print("Images found:", len(images))
        print("Buttons found:", len(buttons))

        assert len(images) + len(buttons) > 0
    finally:
        driver.quit()
