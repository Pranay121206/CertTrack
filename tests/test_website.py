
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


WEBSITE = (
    "file:///C:/Users/Pranay/OneDrive/Desktop/"
    "wt-fixed/wt-fixed/index.html"
)


def test_empty_login_fields_show_error():
    options = Options()
    options.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=options)

    try:
        driver.get(WEBSITE)

        # Clear both login fields
        driver.find_element(By.ID, "le").clear()
        driver.find_element(By.ID, "lp").clear()

        # Submit the empty login form
        driver.find_element(
            By.CSS_SELECTOR, "#lf button[type='submit']"
        ).click()

        # Locate the message text, not the toast icon
        error = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, ".toast .tm")
            )
        )

        assert "Invalid email or password" in error.text

        print("Empty login rejected correctly.")
        print("Error message:", error.text)

    finally:
        driver.quit()


def test_invalid_login_shows_error():
    options = Options()
    options.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=options)

    try:
        driver.get(WEBSITE)

        driver.find_element(By.ID, "le").send_keys(
            "invalid@example.com"
        )
        driver.find_element(By.ID, "lp").send_keys(
            "wrongpassword"
        )
        driver.find_element(
            By.CSS_SELECTOR, "#lf button[type='submit']"
        ).click()

        error = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, ".toast .tm")
            )
        )

        assert "Invalid email or password" in error.text
        print("Invalid login rejected correctly.")

    finally:
        driver.quit()
