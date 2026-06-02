import os
import pytest
from unittest.mock import MagicMock, patch

@patch("src.main.init_db")
@patch("src.main.create_app")
@patch("src.main.start_scheduler")
@patch("src.main.ApplicationBuilder") # mocked inside create_app if importing there?
# Actually main imports create_app from telegram_bot.
def test_main_wiring(mock_application_builder, mock_start_sched, mock_create_app, mock_init_db):
    from src.main import main
    
    # Set env
    os.environ["TELEGRAM_BOT_TOKEN"] = "TEST_TOKEN"
    os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    
    # Mock create_app return value
    mock_app = MagicMock()
    mock_create_app.return_value = mock_app
    
    # Run main
    # main calls app.run_polling which blocks. We need to mock run_polling.
    mock_app.run_polling = MagicMock()
    
    main()
    
    mock_init_db.assert_called_once()
    mock_create_app.assert_called_once_with("TEST_TOKEN")
    
    # Check if post_init was assigned a function (not a Mock)
    from unittest.mock import Mock
    assert not isinstance(mock_app.post_init, Mock), "post_init should be a real function"
    
    # Check run_polling called
    mock_app.run_polling.assert_called_once()
