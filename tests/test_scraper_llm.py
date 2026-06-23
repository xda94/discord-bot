from unittest.mock import patch

from scraper import PriceScraper

def test_extract_with_llm_success():
    scraper = PriceScraper()
    
    mock_response = '{"title": "Awesome Product", "price": 99.99, "currency": "USD", "in_stock": true}'
    
    with patch('scraper.query_ollama', return_value=mock_response) as mock_query:
        price, title, currency, in_stock = scraper._extract_with_llm("Some dummy text")
        
        mock_query.assert_called_once()
        assert price == 99.99
        assert title == "Awesome Product"
        assert currency == "USD"
        assert in_stock is True

def test_extract_with_llm_invalid_json():
    scraper = PriceScraper()
    
    mock_response = 'not json'
    
    with patch('scraper.query_ollama', return_value=mock_response) as mock_query:
        price, title, currency, in_stock = scraper._extract_with_llm("Some dummy text")
        
        mock_query.assert_called_once()
        assert price is None
        assert title is None
        assert currency is None
        assert in_stock is None

def test_extract_with_llm_truncates_long_text():
    scraper = PriceScraper()
    
    # 3005 words
    long_text = "word " * 3005
    mock_response = '{"title": "Awesome Product"}'
    
    with patch('scraper.query_ollama', return_value=mock_response) as mock_query:
        scraper._extract_with_llm(long_text)
        
        # Check that the text was truncated in the prompt
        args, kwargs = mock_query.call_args
        prompt = args[0]
        # the text passed to the LLM should contain 3000 words + some prompt text
        # the prompt text has about 60 words
        assert len(prompt.split()) < 3100 
        
