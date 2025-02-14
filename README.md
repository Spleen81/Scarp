Description
-----------
This Python script allows searching and retrieving scientific articles from multiple academic databases, including:
- ArXiv
- PubMed
- NCBI Gene
- Europe PMC
- Scopus

The program implements a rate-limiting system to prevent exceeding API limits and supports multiple output formats (JSON, CSV, TXT).

Requirements
------------
This script requires **Python 3.7+** and the following libraries:
- aiohttp
- asyncio
- argparse
- json
- csv
- xml.etree.ElementTree
- datetime
- aiofiles

You can install them with:

pip install aiohttp aiofiles


Usage
-----
The script can be executed from the command line with various options.

Basic Execution:

python scrap.py --query "astrobiology exoplanets" --max_results 10 --format json

Retrieves 10 articles related to "astrobiology exoplanets" and saves them in a JSON file.

Using a Query File:

python scrap.py --queries_file queries.txt --max_results 20 --format csv

Loads multiple queries from `queries.txt`, retrieves up to 20 articles per query, and saves the results in CSV format.

Selecting a Specific Database:

python scrap.py --query "life detection" --source pubmed --max_results 5

Limits the search to PubMed and downloads up to 5 articles.

Filtering Results:

python scrap.py --query "astrobiology" --filter "Mars"

Filters results based on the keyword "Mars" in the title, abstract, or author list.

Code Structure
--------------
The script is structured into the following modules:
- `Article`: Defines the structure of the articles.
- `RateLimitedSession`: Manages API requests while respecting rate limits.
- `ArticleFetcher`: Abstract class for article retrieval.
- `ArxivFetcher`, `PubMedFetcher`, `EuropePMCFetcher`, `ScopusFetcher`, `GeneFetcher`: Implementations for specific APIs.
- `ResultsExporter`: Handles exporting results in different formats.
- `main`: Parses arguments and manages asynchronous execution of searches.

Example Output (TXT Format)
---------------------------

Title: Life on Mars: Past, Present, and Future Perspectives
Authors: John Doe, Jane Smith
Abstract: This study explores the possibility of microbial life on Mars...
URL: https://pubmed.ncbi.nlm.nih.gov/12345678
Source: PubMed
Retrieved: 2025-02-14T12:34:56
DOI: 10.1001/marslife.2025.1234
Keywords: Mars, Astrobiology, Microbial Life
--------------------------------------------


Notes
-----
- Some APIs (e.g., Scopus) require an API key.
- Rate-limiting wait times vary by database.
- Data is saved with a filename based on the query and timestamp.

