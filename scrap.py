import os
import json
import csv
import asyncio
import aiohttp
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Union, Optional
from dataclasses import dataclass
from abc import ABC, abstractmethod
import xml.etree.ElementTree as ET
import aiofiles

@dataclass
class Article:
    """Standardized structure for scientific articles across different sources."""
    title: str
    authors: List[str]
    abstract: str
    url: str
    source: str
    retrieved_at: datetime
    doi: Optional[str] = None
    keywords: List[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "url": self.url,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "doi": self.doi,
            "keywords": self.keywords
        }

class APIError(Exception):
    """Custom exception for API-related errors"""
    pass

class RateLimitedSession:
    """Manages rate limits for different APIs with configurable limits and backoff."""
    
    def __init__(self, config: Dict[str, tuple]):
        self.session = aiohttp.ClientSession()
        self.limits = config
        self.timestamps = {k: [] for k in self.limits.keys()}
        self.timeout = aiohttp.ClientTimeout(total=30)
        self.retry_delays = {k: 1 for k in self.limits.keys()}  # Initial delay in seconds
        
    async def request(self, api: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Execute a rate-limited request with exponential backoff retry mechanism."""
        if api not in self.limits:
            raise ValueError(f"Unknown API: {api}")
            
        limit, period = self.limits[api]
        max_retries = 3
        current_retry = 0
        
        while current_retry <= max_retries:
            try:
                now = datetime.now()
                
                # Clean old timestamps
                self.timestamps[api] = [ts for ts in self.timestamps[api] 
                                      if now - ts < timedelta(seconds=period)]
                
                # Wait if necessary
                while len(self.timestamps[api]) >= limit:
                    wait_time = period - (now - min(self.timestamps[api])).seconds + 1
                    print(f"Rate limit reached for {api}. Waiting {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    now = datetime.now()
                    self.timestamps[api] = [ts for ts in self.timestamps[api] 
                                          if now - ts < timedelta(seconds=period)]
                
                kwargs['timeout'] = self.timeout
                response = await self.session.get(url, **kwargs)
                
                if response.status == 429:  # Too Many Requests
                    raise APIError("Rate limit exceeded")
                    
                response.raise_for_status()
                self.timestamps[api].append(now)
                
                # Reset retry delay on successful request
                self.retry_delays[api] = 1
                return response
                
            except (APIError, aiohttp.ClientError) as e:
                current_retry += 1
                if current_retry > max_retries:
                    raise APIError(f"Error accessing {api} API after {max_retries} retries: {str(e)}")
                
                # Exponential backoff
                delay = self.retry_delays[api]
                print(f"Attempt {current_retry} failed for {api}. Waiting {delay} seconds before retry...")
                await asyncio.sleep(delay)
                self.retry_delays[api] = min(delay * 2, 60)  # Double delay, max 60 seconds
    
    async def close(self):
        await self.session.close()

class ArticleFetcher(ABC):
    """Abstract base class for article fetching with validation."""
    
    def __init__(self, session: RateLimitedSession):
        self.session = session
        
    @abstractmethod
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        """Fetch articles from the source."""
        pass
    
    def validate_query(self, query: str) -> None:
        """Validate the search query."""
        if not query or len(query.strip()) == 0:
            raise ValueError("Search query cannot be empty")
        if len(query) > 256:
            raise ValueError("Search query too long")
            
class ArxivFetcher(ArticleFetcher):
    """Fetches articles from ArXiv using their API."""
    
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        self.validate_query(query)
        base_url = "http://export.arxiv.org/api/query"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending"
        }
        
        try:
            async with await self.session.request("arxiv", base_url, params=params) as response:
                text = await response.text()
                root = ET.fromstring(text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                
                articles = []
                for entry in root.findall("atom:entry", ns):
                    # Extract DOI if available
                    doi = None
                    for link in entry.findall("atom:link", ns):
                        if link.get("title") == "doi":
                            doi = link.get("href").split("doi.org/")[-1]
                            break
                    
                    articles.append(Article(
                        title=entry.find("atom:title", ns).text.strip(),
                        authors=[author.find("atom:name", ns).text 
                                for author in entry.findall("atom:author", ns)],
                        abstract=entry.find("atom:summary", ns).text.strip(),
                        url=entry.find("atom:link[@rel='alternate']", ns).attrib["href"],
                        source="arxiv",
                        retrieved_at=datetime.now(),
                        doi=doi,
                        keywords=[cat.get("term") for cat in entry.findall("atom:category", ns)]
                    ))
                
                return articles
                
        except ET.ParseError as e:
            raise APIError(f"Error parsing ArXiv response: {str(e)}")
            
class ScopusFetcher(ArticleFetcher):
    """Fetches articles from Scopus using their official API."""
    
    def __init__(self, session: RateLimitedSession):
        super().__init__(session)
        self.api_key = "d6007cfbc0d8ad9ad102e1910bbdd0f7"  # API key inserita direttamente
        self.base_url = "https://api.elsevier.com/content/search/scopus"
        
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        self.validate_query(query)
        
        headers = {
            "X-ELS-APIKey": self.api_key,
            "Accept": "application/json"
        }
        
        params = {
            "query": query,
            "count": min(max_results, 25),  # Scopus limits per request
            "start": 0,
            "sort": "-citedby-count",  # Sort by citation count descending
            "field": "title,creator,description,doi,identifier,keyword"
        }
        
        try:
            async with await self.session.request("scopus", self.base_url, 
                                                params=params, headers=headers) as response:
                data = await response.json()
                
                if "service-error" in data:
                    raise APIError(f"Scopus API error: {data['service-error']}")
                
                results = data.get("search-results", {}).get("entry", [])
                articles = []
                
                for result in results:
                    # Extract authors - Scopus provides them in 'creator' field
                    authors = []
                    creator = result.get("creator", "")
                    if creator:
                        authors = [author.strip() for author in creator.split(";")]
                    
                    # Extract DOI
                    doi = result.get("prism:doi")
                    
                    # Extract keywords
                    keywords = []
                    if "authkeywords" in result:
                        keywords = [kw.strip() for kw in result["authkeywords"].split("|")]
                    
                    # Create article object
                    articles.append(Article(
                        title=result.get("dc:title", "N/A"),
                        authors=authors,
                        abstract=result.get("dc:description", "No abstract available"),
                        url=result.get("prism:url", ""),
                        source="scopus",
                        retrieved_at=datetime.now(),
                        doi=doi,
                        keywords=keywords
                    ))
                
                return articles
                
        except Exception as e:
            raise APIError(f"Error fetching from Scopus: {str(e)}")

class PubMedFetcher(ArticleFetcher):
    """Fetches articles from PubMed using their E-utilities API with improved rate limiting."""
    
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        self.validate_query(query)
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        
        # Reduce batch size for better rate limit handling
        batch_size = 5
        total_fetched = 0
        articles = []
        
        while total_fetched < max_results:
            current_batch = min(batch_size, max_results - total_fetched)
            params = {
                "db": "pubmed",
                "term": query,
                "retmax": current_batch,
                "retstart": total_fetched,
                "retmode": "json"
            }
            
            try:
                async with await self.session.request("pubmed", base_url, params=params) as response:
                    data = await response.json()
                    ids = data.get("esearchresult", {}).get("idlist", [])
                    
                    # Fetch details in smaller batches
                    for pmid in ids:
                        # Add delay between individual article fetches
                        await asyncio.sleep(0.5)
                        
                        fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                        fetch_params = {
                            "db": "pubmed",
                            "id": pmid,
                            "retmode": "json"
                        }
                        
                        async with await self.session.request("pubmed", fetch_url, params=fetch_params) as fetch_response:
                            details = await fetch_response.json()
                            article_data = details["result"][pmid]
                            
                            # [Rest of the article processing remains the same]
                            authors = []
                            for author in article_data.get("authors", []):
                                if isinstance(author, dict) and "name" in author:
                                    authors.append(author["name"])
                                elif isinstance(author, str):
                                    authors.append(author)
                            
                            doi = None
                            if "articleids" in article_data:
                                for id_obj in article_data["articleids"]:
                                    if id_obj.get("idtype") == "doi":
                                        doi = id_obj.get("value")
                                        break
                            
                            articles.append(Article(
                                title=article_data.get("title", "N/A"),
                                authors=authors,
                                abstract=article_data.get("abstract", "N/A"),
                                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}",
                                source="pubmed",
                                retrieved_at=datetime.now(),
                                doi=doi,
                                keywords=article_data.get("keywords", [])
                            ))
                
                total_fetched += len(ids)
                if len(ids) < current_batch:  # No more results available
                    break
                    
                # Add delay between batches
                await asyncio.sleep(1)
                
            except (KeyError, json.JSONDecodeError) as e:
                raise APIError(f"Error processing PubMed response: {str(e)}")
        
        return articles

class EuropePMCFetcher(ArticleFetcher):
    """Fetches articles from Europe PMC using their API."""
    
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        self.validate_query(query)
        base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        params = {
            "query": query,
            "format": "json",
            "pageSize": max_results,
            "resultType": "core"
        }
        
        try:
            async with await self.session.request("europepmc", base_url, params=params) as response:
                data = await response.json()
                articles = []
                
                for result in data.get("resultList", {}).get("result", []):
                    # Estrai DOI e autori, gestendo assenza di campi
                    doi = result.get("doi", None)
                    authors = []
                    if "authorList" in result and "author" in result["authorList"]:
                        authors = [
                            author.get("fullName", "Unknown Author") 
                            for author in result["authorList"]["author"]
                            if isinstance(author, dict)
                        ]
                    
                    articles.append(Article(
                        title=result.get("title", "N/A"),
                        authors=authors,
                        abstract=result.get("abstractText", "No abstract available"),
                        url=f"https://europepmc.org/article/{result.get('source', 'N/A')}/{result.get('id', 'N/A')}",
                        source="europe_pmc",
                        retrieved_at=datetime.now(),
                        doi=doi,
                        keywords=result.get("keywordList", {}).get("keyword", [])
                    ))
                
                return articles
        
        except (KeyError, json.JSONDecodeError) as e:
            raise APIError(f"Error processing Europe PMC response: {str(e)}")


class GeneFetcher(ArticleFetcher):
    """Fetches gene information from NCBI Gene database."""
    
    async def fetch_articles(self, query: str, max_results: int) -> List[Article]:
        self.validate_query(query)
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {
            "db": "gene",
            "term": query,
            "retmax": max_results,
            "retmode": "json"
        }
        
        try:
            articles = []
            async with await self.session.request("ncbi", base_url, params=params) as response:
                data = await response.json()
                ids = data.get("esearchresult", {}).get("idlist", [])
                
                for gene_id in ids:
                    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                    fetch_params = {
                        "db": "gene",
                        "id": gene_id,
                        "retmode": "json"
                    }
                    
                    async with await self.session.request("ncbi", fetch_url, params=fetch_params) as fetch_response:
                        details = await fetch_response.json()
                        gene_data = details["result"][gene_id]
                        
                        articles.append(Article(
                            title=gene_data.get("description", "N/A"),
                            authors=[gene_data.get("organism", {}).get("scientificname", "N/A")],
                            abstract=f"Symbol: {gene_data.get('symbol', 'N/A')}\n"
                                    f"Type: {gene_data.get('type', 'N/A')}\n"
                                    f"Location: {gene_data.get('maplocation', 'N/A')}\n"
                                    f"Summary: {gene_data.get('summary', 'N/A')}",
                            url=f"https://www.ncbi.nlm.nih.gov/gene/{gene_id}",
                            source="ncbi_gene",
                            retrieved_at=datetime.now(),
                            keywords=[gene_data.get('type', 'N/A')]
                        ))
            
            return articles
            
        except (KeyError, json.JSONDecodeError) as e:
            raise APIError(f"Error processing NCBI Gene response: {str(e)}")

class ResultsExporter:
    """Handles exporting results in different formats with error handling."""
    
    @staticmethod
    async def export(articles: List[Article], filename: str, format: str):
        try:
            if format == "json":
                async with aiofiles.open(f"{filename}.json", 'w', encoding='utf-8') as f:
                    await f.write(json.dumps([a.to_dict() for a in articles], 
                                             indent=2, ensure_ascii=False))
            elif format == "csv":
                fieldnames = list(Article.__annotations__.keys())
                with open(f"{filename}.csv", 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    for article in articles:
                        writer.writerow(article.to_dict())
            elif format == "txt":
                async with aiofiles.open(f"{filename}.txt", 'w', encoding='utf-8') as f:
                    for article in articles:
                        await f.write(f"Title: {article.title}\n")
                        await f.write(f"Authors: {', '.join(article.authors)}\n")
                        await f.write(f"Abstract: {article.abstract}\n")
                        await f.write(f"URL: {article.url}\n")
                        await f.write(f"Source: {article.source}\n")
                        await f.write(f"Retrieved: {article.retrieved_at}\n")
                        if article.doi:
                            await f.write(f"DOI: {article.doi}\n")
                        if article.keywords:
                            valid_keywords = [kw for kw in article.keywords if kw]  # Filtra None
                            await f.write(f"Keywords: {', '.join(valid_keywords)}\n")
                        else:
                            await f.write("Keywords: None\n")
                        await f.write("\n" + "-"*80 + "\n")
        except IOError as e:
            raise IOError(f"Error exporting results: {str(e)}")

def load_queries(filename: str) -> List[str]:
    """Legge le query da un file di testo."""
    with open(filename, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


async def main():
    parser = argparse.ArgumentParser(
        description="Search and filter scientific articles across multiple databases.\n"
                    "You can provide a single query using --query or a file containing multiple queries using --queries_file.\n"
                    "If both are provided, --queries_file takes precedence.",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--queries_file", type=str,
                        help="Path to a file containing multiple queries (one per line).")
    parser.add_argument("--query", type=str,
                        help="Single search query (overridden by --queries_file if both are provided).")
    parser.add_argument("--max_results", type=int, default=10,
                        help="Maximum number of articles to retrieve per query (default: 10).")
    parser.add_argument("--format", choices=["json", "csv", "txt"],
                        default="txt", help="Output file format (default: txt).")
    parser.add_argument("--output", type=str, 
                        help="Prefix for the output filename (default: query + timestamp).")
    parser.add_argument("--source", 
                        choices=["arxiv", "pubmed", "gene", "europepmc", "scopus", "all"],
                        default="default", help="Source database to search (default: arxiv, europepmc, scopus).")
    parser.add_argument("--filter", type=str, 
                        help="Optional keyword to filter results by title, abstract, or author.")

    args = parser.parse_args()

    # Validazioni
    if not args.query and not args.queries_file:
        raise ValueError("You must provide either --query or --queries_file.")

    # Carica le query
    if args.queries_file:
        queries = load_queries(args.queries_file)
    else:
        queries = [args.query]

    # Rimuovi eventuali query vuote
    queries = [q for q in queries if q.strip()]
    if not queries:
        raise ValueError("No valid queries found in the input.")

    session = None
    try:
        # Configura la sessione con rate limiting
        session = RateLimitedSession({
            "arxiv": (60, 3600),      # 60 requests per hour
            "pubmed": (1, 2),         # 1 request every 2 seconds
            "ncbi": (1, 2),           # 1 request every 2 seconds
            "europepmc": (300, 60),   # 300 requests per minute
            "scopus": (2, 1)          # 2 requests per second
        })

        for query in queries:
            print(f"\nStarting search for '{query}'...")
            tasks = []
            fetchers = []

            # Determina quali sorgenti usare
            if args.source == "default":
                # Default sources: solo arxiv, europepmc, e scopus (togliamo la roba "bio" agli astrofisici :)
                sources = ["arxiv", "europepmc", "scopus"]
            elif args.source == "all":
                # All sources
                sources = ["arxiv", "pubmed", "gene", "europepmc", "scopus"]
            else:
                # Specific source
                sources = [args.source]

            # Aggiungi i fetcher in base alle sorgenti selezionate
            if "arxiv" in sources:
                fetchers.append(("ArXiv", ArxivFetcher(session)))
            if "pubmed" in sources:
                fetchers.append(("PubMed", PubMedFetcher(session)))
            if "gene" in sources:
                fetchers.append(("NCBI Gene", GeneFetcher(session)))
            if "europepmc" in sources:
                fetchers.append(("Europe PMC", EuropePMCFetcher(session)))
            if "scopus" in sources:
                fetchers.append(("Scopus", ScopusFetcher(session)))

            for source_name, fetcher in fetchers:
                task = fetcher.fetch_articles(query, args.max_results)
                tasks.append(task)
            
            # Esegui le ricerche in parallelo
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_articles = []
            for (source_name, _), result in zip(fetchers, results):
                if isinstance(result, Exception):
                    print(f"Error fetching from {source_name}: {str(result)}")
                else:
                    all_articles.extend(result)
            # Applica il filtro se specificato
            if args.filter:
                print(f"\nApplying filter: '{args.filter}'")
                original_count = len(all_articles)
                all_articles = [article for article in all_articles 
                                if (args.filter.lower() in article.title.lower() or
                                    args.filter.lower() in article.abstract.lower() or
                                    any(args.filter.lower() in author.lower() 
                                        for author in article.authors))]
                print(f"Filtered results: {len(all_articles)} articles (removed {original_count - len(all_articles)})")

            # Salva i risultati immediatamente
            if all_articles:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_query = query.replace(" ", "_").replace("/", "_").replace("\\", "_")
                filter_suffix = f"_filter_{args.filter}" if args.filter else ""
                output_file = f"{args.output or safe_query}_{timestamp}{filter_suffix}"
                await ResultsExporter.export(all_articles, output_file, args.format)
                print(f"Successfully exported articles for '{query}' to: {output_file}.{args.format}")
            else:
                print(f"No articles found for query: '{query}'")
            
            # Conferma completamento della query prima di passare alla successiva
            print(f"Completed processing for query: '{query}'\n")
    finally:
        if session:
            await session.close()


if __name__ == "__main__":
    asyncio.run(main())