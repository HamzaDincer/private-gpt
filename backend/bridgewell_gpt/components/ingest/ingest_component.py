import abc
import itertools
import logging
import multiprocessing
import multiprocessing.pool
import os
import threading
from pathlib import Path
from queue import Queue
from typing import Any
import uuid
import json
import math

from llama_index.core.data_structs import IndexDict
from llama_index.core.embeddings.utils import EmbedType
from llama_index.core.indices import VectorStoreIndex, load_index_from_storage
from llama_index.core.indices.base import BaseIndex
from llama_index.core.ingestion import run_transformations
from llama_index.core.schema import BaseNode, Document, TransformComponent
from llama_index.core.storage import StorageContext

from bridgewell_gpt.components.extraction.extraction_component import ExtractionComponent
from bridgewell_gpt.components.ingest.ingest_helper import IngestionHelper
from bridgewell_gpt.paths import local_data_path
from bridgewell_gpt.settings.settings import Settings
from bridgewell_gpt.utils.eta import eta
from bridgewell_gpt.server.document_types.document_type_service import DocumentTypeService

logger = logging.getLogger(__name__)


class BaseIngestComponent(abc.ABC):
    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        logger.debug("Initializing base ingest component type=%s", type(self).__name__)
        self.storage_context = storage_context
        self.embed_model = embed_model
        self.transformations = transformations

    @abc.abstractmethod
    def ingest(self, file_name: str, file_data: Path) -> list[Document]:
        pass

    @abc.abstractmethod
    def bulk_ingest(self, files: list[tuple[str, Path]]) -> list[Document]:
        pass

    @abc.abstractmethod
    def delete(self, doc_id: str) -> None:
        pass


class BaseIngestComponentWithIndex(BaseIngestComponent, abc.ABC):
    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(storage_context, embed_model, transformations, *args, **kwargs)

        self.show_progress = True
        self._index_thread_lock = (
            threading.Lock()
        )  # Thread lock! Not Multiprocessing lock
        self._index = self._initialize_index()

    def _initialize_index(self) -> BaseIndex[IndexDict]:
        """Initialize the index from the storage context."""
        try:
            # Load the index with store_nodes_override=True to be able to delete them
            index = load_index_from_storage(
                storage_context=self.storage_context,
                store_nodes_override=True,  # Force store nodes in index and document stores
                show_progress=self.show_progress,
                embed_model=self.embed_model,
                transformations=self.transformations,
            )
        except ValueError:
            # There are no index in the storage context, creating a new one
            logger.info("Creating a new vector store index")
            index = VectorStoreIndex.from_documents(
                [],
                storage_context=self.storage_context,
                store_nodes_override=True,  # Force store nodes in index and document stores
                show_progress=self.show_progress,
                embed_model=self.embed_model,
                transformations=self.transformations,
            )
            index.storage_context.persist(persist_dir=local_data_path)
        return index

    def _save_index(self) -> None:
        self._index.storage_context.persist(persist_dir=local_data_path)

    def delete(self, doc_id: str) -> None:
        with self._index_thread_lock:
            # Delete the document from the index
            self._index.delete_ref_doc(doc_id, delete_from_docstore=True)

            # Save the index
            self._save_index()


class SimpleIngestComponent(BaseIngestComponentWithIndex):
    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        extraction_component: ExtractionComponent,
        ingest_service: Any,  # Type hint as Any to avoid circular import
        chat_service: Any = None,  # Add chat service parameter
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(storage_context, embed_model, transformations, *args, **kwargs)
        self.extraction_component = extraction_component
        self._rag_thread_lock = threading.Lock()
        self._ingest_service = ingest_service
        self._chat_service = chat_service

    def ingest(self, file_name: str, file_data: Path) -> list[Document]:
        logger.info("Ingesting file_name=%s", file_name)
        stored_file_path = IngestionHelper.store_original_file(file_name, file_data)
        import uuid
        doc_id = str(uuid.uuid4())
        # Return a stub Document object for compatibility
        doc = Document(text="", metadata={"file_name": file_name, "doc_id": doc_id})
        doc.doc_id = doc_id  # Explicitly set the attribute
        self._background_save_docs(file_name, stored_file_path, doc_id)
        return [doc]

    def _background_save_docs(self, file_name: str, stored_file_path: Path, doc_id: str) -> None:
        """Save documents to index in the background and perform RAG extraction."""
        import threading
        import time
        # Remove the early phase update here
        # DocumentTypeService().update_document_phase(doc_id, "parsing")
        def update_phase(doc_id: str, phase: str):
            DocumentTypeService().update_document_phase(doc_id, phase)
            logger.info(f"[PHASE UPDATE] doc_id={doc_id} phase={phase}")

        def wait_for_document_entry(doc_id, timeout=10):
            start = time.time()
            while time.time() - start < timeout:
                data = DocumentTypeService()._load_data()
                for type_data in data:
                    for doc in type_data.get('documents', []):
                        if doc['id'] == doc_id:
                            return True
                time.sleep(0.2)
            return False

        def parse_documents():
            with self._index_thread_lock:
                documents = IngestionHelper.transform_file_into_documents(file_name, stored_file_path)
                logger.info(
                    "Transformed file=%s into count=%s documents", file_name, len(documents)
                )
                # Generate document IDs before extraction/embedding
                for doc in documents:
                    doc.doc_id = doc_id
                    if not doc.metadata:
                        doc.metadata = {}
                    doc.metadata["file_name"] = file_name
                    doc.metadata["doc_id"] = doc_id
            return documents

        def run_extraction(documents):
            update_phase(doc_id, "extraction")
            extraction = self.extraction_component.extract_document(documents, "Benefit", file_name, doc_id)
            logger.debug(f"Extraction: {extraction}")
            update_phase(doc_id, "embedding")
            # Store extraction result in document metadata
            if extraction and extraction.get("status") == "completed":
                logger.info(f"Initial extraction successful with ID: {extraction.get('extraction_id')}")
                for doc in documents:
                    if not doc.metadata:
                        doc.metadata = {}
                    doc.metadata["extraction"] = extraction.get("result", {})
                    doc.metadata["extraction_id"] = extraction.get("extraction_id")
                    doc.metadata["document_type"] = extraction.get("document_type")
                    logger.debug(f"Set extraction_id={doc.metadata['extraction_id']} for doc_id={doc.doc_id}")

        def run_embedding(documents):         
            # Transform each document individually to avoid shared metadata
            all_nodes = []
            for doc in documents:
                nodes = run_transformations(
                    [doc],  # single document at a time
                    self.transformations,
                    show_progress=False,
                )
                all_nodes.extend(nodes)
            batch_size = getattr(self.embed_model, 'batch_size', 32)
            n_batches = math.ceil(len(all_nodes) / batch_size)
            logger.info(f"Embedding {len(all_nodes)} nodes in {n_batches} batches (batch_size={batch_size})")
            for i, node_batch in enumerate(batch_nodes(all_nodes, batch_size)):
                logger.info(f"Inserting embedding batch {i+1}/{n_batches} (size {len(node_batch)})")
                self._index.insert_nodes(node_batch, show_progress=True)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            logger.debug("Persisting the index and nodes")
            self._save_index()
            logger.debug("Persisted the index and nodes")

        def save_task(doc_id_arg=doc_id):
            doc_id = doc_id_arg
            try:
                # Wait for document entry to exist before updating phase
                wait_for_document_entry(doc_id)
                update_phase(doc_id, "parsing")
                documents = parse_documents()
                # Start extraction and embedding in parallel
                extraction_thread = threading.Thread(target=run_extraction, args=(documents,))
                embedding_thread = threading.Thread(target=run_embedding, args=(documents,))
                extraction_thread.start()
                embedding_thread.start()
                extraction_thread.join()
                embedding_thread.join()
                # After both are done, proceed with RAG extraction as before
                logger.info("Starting RAG extraction after embeddings and extraction")
                update_phase(doc_id, "rag")
                with self._rag_thread_lock:
                    try:
                        # Get the file name from the first document's metadata
                        doc_file_name = documents[0].metadata.get("file_name")
                        if not doc_file_name:
                            logger.warning("No file name found in document metadata")
                            return

                        # Get the extraction result from metadata
                        logger.info(f"Using doc_id={doc_id} for RAG extraction")
                        if not doc_id:
                            logger.warning("No doc_id available for RAG extraction")
                            return

                        # Get document type for company config
                        document_type = documents[0].metadata.get("document_type")
                        if not document_type:
                            logger.warning("No document type found in document metadata")
                            return

                        # Load the extraction result file
                        result_file = local_data_path / "extraction_results" / doc_id / "result.json"
                        logger.info(f"Looking for result file at: {result_file}")
                        if not result_file.exists():
                            logger.warning(f"Extraction result file not found: {result_file}")
                            return

                        with open(result_file) as f:
                            extraction_data = json.load(f)

                        extraction_result = extraction_data.get("result", {})
                        if not extraction_result:
                            logger.warning("No extraction result found in result file")
                            return

                        # Check for missing fields
                        from bridgewell_gpt.server.extraction.extraction_service import ExtractionService
                        from bridgewell_gpt.server.extraction.insurance_schema import InsuranceSummary
                        
                        # Initialize extraction service with both ingest and chat services
                        extraction_service = ExtractionService(self._ingest_service, self._chat_service)
                        missing_fields = extraction_service._has_missing_fields(extraction_result, InsuranceSummary)

                        def batch_fields(fields, batch_size):
                            for i in range(0, len(fields), batch_size):
                                yield fields[i:i+batch_size]

                        if missing_fields:
                            logger.info(f"Found {len(missing_fields)} missing fields, using RAG to extract them in batches of 10")
                            company_config = extraction_service._load_company_config("general")  # Default to general config for now
                            all_rag_results = {}
                            batch_size = 10
                            for batch in batch_fields(missing_fields, batch_size):
                                rag_results = extraction_service._extract_with_rag(
                                    doc_id=doc_id,
                                    missing_fields=batch,
                                    company_config=company_config
                                )
                                # Merge this batch's results into all_rag_results
                                def deep_update(d: dict, u: dict) -> dict:
                                    if not isinstance(u, dict):
                                        return d
                                    for k, v in u.items():
                                        if isinstance(v, dict):
                                            if not isinstance(d.get(k), dict) or not d.get(k):
                                                d[k] = v
                                            else:
                                                d[k] = deep_update(d.get(k, {}), v)
                                        else:
                                            d[k] = v
                                    return d
                                all_rag_results = deep_update(all_rag_results, rag_results)
                            # Log RAG results before updating
                            logger.info("RAG batch full results: %s", json.dumps(all_rag_results, indent=2))
                            # Update extraction result with all RAG results
                            if isinstance(extraction_result, dict) and isinstance(all_rag_results, dict):
                                final_data = deep_update(extraction_result, all_rag_results)
                                extraction_data["result"] = final_data
                                with open(result_file, 'w') as f:
                                    json.dump(extraction_data, f, indent=2)
                                logger.info("Successfully updated extraction results with batched RAG data")
                            else:
                                logger.error(f"Cannot update extraction results: extraction_result is {type(extraction_result)}, all_rag_results is {type(all_rag_results)}")
                        else:
                            logger.info("No missing fields found, skipping RAG extraction")

                        # After RAG (or if skipped), mark as completed
                        update_phase(doc_id, "completed")
                    except Exception as e:
                        logger.error(f"Error during RAG extraction: {str(e)}")
                        update_phase(doc_id, "completed")
            except Exception as e:
                logger.error(f"Error in save task: {str(e)}")
                if doc_id is not None:
                    update_phase(doc_id, "error")
            finally:
                if doc_id is not None:
                    update_phase(doc_id, "completed")

        thread = threading.Thread(target=save_task, args=(doc_id,))
        thread.start()

    def bulk_ingest(self, files: list[tuple[str, Path]]) -> list[Document]:
        saved_documents = []
        for file_name, file_data in files:
            documents = IngestionHelper.transform_file_into_documents(
                file_name, file_data
            )
            saved_documents.extend(self._save_docs(documents))
        return saved_documents

    def _save_docs(self, documents: list[Document]) -> list[Document]:
        logger.debug("Transforming count=%s documents into nodes", len(documents))
        nodes = run_transformations(
            documents,  # type: ignore[arg-type]
            self.transformations,
            show_progress=self.show_progress,
        )
        batch_size = getattr(self.embed_model, 'embed_batch_size', 32)
        n_batches = math.ceil(len(nodes) / batch_size)
        with self._index_thread_lock:
            logger.info("Inserting count=%s nodes in the index (batch_size=%s, n_batches=%s)", len(nodes), batch_size, n_batches)
            for i, node_batch in enumerate(batch_nodes(nodes, batch_size)):
                logger.info(f"Inserting embedding batch {i+1}/{n_batches} (size {len(node_batch)})")
                self._index.insert_nodes(node_batch, show_progress=False)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            logger.debug("Persisting the index and nodes")
            self._save_index()
            logger.debug("Persisted the index and nodes")
        return documents


class BatchIngestComponent(BaseIngestComponentWithIndex):
    """Parallelize the file reading and parsing on multiple CPU core.

    This also makes the embeddings to be computed in batches (on GPU or CPU).
    """

    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        count_workers: int,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(storage_context, embed_model, transformations, *args, **kwargs)
        # Make an efficient use of the CPU and GPU, the embedding
        # must be in the transformations
        assert (
            len(self.transformations) >= 2
        ), "Embeddings must be in the transformations"
        assert count_workers > 0, "count_workers must be > 0"
        self.count_workers = count_workers

        self._file_to_documents_work_pool = multiprocessing.Pool(
            processes=self.count_workers
        )

    def ingest(self, file_name: str, file_data: Path) -> list[Document]:
        logger.info("Ingesting file_name=%s", file_name)
        documents = IngestionHelper.transform_file_into_documents(file_name, file_data)
        logger.info(
            "Transformed file=%s into count=%s documents", file_name, len(documents)
        )
        logger.debug("Saving the documents in the index and doc store")
        return self._save_docs(documents)

    def bulk_ingest(self, files: list[tuple[str, Path]]) -> list[Document]:
        documents = list(
            itertools.chain.from_iterable(
                self._file_to_documents_work_pool.starmap(
                    IngestionHelper.transform_file_into_documents, files
                )
            )
        )
        logger.info(
            "Transformed count=%s files into count=%s documents",
            len(files),
            len(documents),
        )
        return self._save_docs(documents)

    def _save_docs(self, documents: list[Document]) -> list[Document]:
        logger.debug("Transforming count=%s documents into nodes", len(documents))
        nodes = run_transformations(
            documents,  # type: ignore[arg-type]
            self.transformations,
            show_progress=self.show_progress,
        )
        batch_size = getattr(self.embed_model, 'embed_batch_size', 32)
        n_batches = math.ceil(len(nodes) / batch_size)
        with self._index_thread_lock:
            logger.info("Inserting count=%s nodes in the index (batch_size=%s, n_batches=%s)", len(nodes), batch_size, n_batches)
            for i, node_batch in enumerate(batch_nodes(nodes, batch_size)):
                logger.info(f"Inserting embedding batch {i+1}/{n_batches} (size {len(node_batch)})")
                self._index.insert_nodes(node_batch, show_progress=False)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            logger.debug("Persisting the index and nodes")
            self._save_index()
            logger.debug("Persisted the index and nodes")
        return documents


class ParallelizedIngestComponent(BaseIngestComponentWithIndex):
    """Parallelize the file ingestion (file reading, embeddings, and index insertion).

    This use the CPU and GPU in parallel (both running at the same time), and
    reduce the memory pressure by not loading all the files in memory at the same time.
    
    Now supports extraction, RAG, and document phase tracking for feature parity with SimpleIngestComponent.
    """

    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        count_workers: int,
        extraction_component: ExtractionComponent = None,  # Add extraction component
        ingest_service: Any = None,  # Add ingest service
        chat_service: Any = None,  # Add chat service
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(storage_context, embed_model, transformations, *args, **kwargs)
        # To make an efficient use of the CPU and GPU, the embeddings
        # must be in the transformations (to be computed in batches)
        assert (
            len(self.transformations) >= 2
        ), "Embeddings must be in the transformations"
        assert count_workers > 0, "count_workers must be > 0"
        self.count_workers = count_workers
        # We are doing our own multiprocessing
        # To do not collide with the multiprocessing of huggingface, we disable it
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        self._ingest_work_pool = multiprocessing.pool.ThreadPool(
            processes=self.count_workers
        )

        self._file_to_documents_work_pool = multiprocessing.Pool(
            processes=self.count_workers
        )
        
        # Add extraction and service components
        self.extraction_component = extraction_component
        self._ingest_service = ingest_service
        self._chat_service = chat_service
        self._rag_thread_lock = threading.Lock()

    def ingest(self, file_name: str, file_data: Path) -> list[Document]:
        logger.info("Ingesting file_name=%s", file_name)
        
        # Store original file
        stored_file_path = IngestionHelper.store_original_file(file_name, file_data)
        
        # Generate doc_id
        doc_id = str(uuid.uuid4())
        
        # Return a stub Document object for compatibility
        doc = Document(text="", metadata={"file_name": file_name, "doc_id": doc_id})
        doc.doc_id = doc_id
        
        # If extraction is enabled, use background processing
        if self.extraction_component:
            self._background_save_docs(file_name, stored_file_path, doc_id)
            return [doc]
        else:
            # Fall back to original parallel processing without extraction
            documents = self._file_to_documents_work_pool.apply(
                IngestionHelper.transform_file_into_documents, (file_name, stored_file_path)
            )
            logger.info(
                "Transformed file=%s into count=%s documents", file_name, len(documents)
            )
            logger.debug("Saving the documents in the index and doc store")
            return self._save_docs(documents)

    def _background_save_docs(self, file_name: str, stored_file_path: Path, doc_id: str) -> None:
        """Save documents to index in the background and perform RAG extraction."""
        import threading
        import time
        # Remove the early phase update here
        # DocumentTypeService().update_document_phase(doc_id, "parsing")
        def update_phase(doc_id: str, phase: str):
            DocumentTypeService().update_document_phase(doc_id, phase)
            logger.info(f"[PHASE UPDATE] doc_id={doc_id} phase={phase}")

        def wait_for_document_entry(doc_id, timeout=10):
            from bridgewell_gpt.server.document_types.document_type_service import DocumentTypeService
            start = time.time()
            while time.time() - start < timeout:
                data = DocumentTypeService()._load_data()
                for type_data in data:
                    for doc in type_data.get('documents', []):
                        if doc['id'] == doc_id:
                            return True
                time.sleep(0.2)
            return False

        def parse_documents():
            with self._index_thread_lock:
                documents = IngestionHelper.transform_file_into_documents(file_name, stored_file_path)
                logger.info(
                    "Transformed file=%s into count=%s documents", file_name, len(documents)
                )
                # Generate document IDs before extraction/embedding
                for doc in documents:
                    doc.doc_id = doc_id
                    if not doc.metadata:
                        doc.metadata = {}
                    doc.metadata["file_name"] = file_name
                    doc.metadata["doc_id"] = doc_id
            return documents

        def run_extraction(documents):
            update_phase(doc_id, "extraction")
            extraction = self.extraction_component.extract_document(documents, "Benefit", file_name, doc_id)
            logger.debug(f"Extraction: {extraction}")
            # Store extraction result in document metadata
            if extraction and extraction.get("status") == "completed":
                logger.info(f"Initial extraction successful with ID: {extraction.get('extraction_id')}")
                for doc in documents:
                    if not doc.metadata:
                        doc.metadata = {}
                    doc.metadata["extraction"] = extraction.get("result", {})
                    doc.metadata["extraction_id"] = extraction.get("extraction_id")
                    doc.metadata["document_type"] = extraction.get("document_type")
                    logger.debug(f"Set extraction_id={doc.metadata['extraction_id']} for doc_id={doc.doc_id}")

        def run_embedding(documents):
            update_phase(doc_id, "embedding")
            # Transform each document individually to avoid shared metadata
            all_nodes = []
            for doc in documents:
                nodes = run_transformations(
                    [doc],  # single document at a time
                    self.transformations,
                    show_progress=False,
                )
                all_nodes.extend(nodes)

            batch_size = getattr(self.embed_model, 'embed_batch_size', 32)
            n_batches = math.ceil(len(all_nodes) / batch_size)
            logger.info(f"Embedding {len(all_nodes)} nodes in {n_batches} batches (batch_size={batch_size})")
            for i, node_batch in enumerate(batch_nodes(all_nodes, batch_size)):
                logger.info(f"Inserting embedding batch {i+1}/{n_batches} (size {len(node_batch)})")
                self._index.insert_nodes(node_batch, show_progress=True)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            self._save_index()
            logger.debug("Persisted the index and nodes")

        def save_task(doc_id_arg=doc_id):
            doc_id = doc_id_arg
            try:
                # Wait for document entry to exist before updating phase
                wait_for_document_entry(doc_id)
                update_phase(doc_id, "parsing")
                documents = parse_documents()
                # Start extraction and embedding in parallel
                extraction_thread = threading.Thread(target=run_extraction, args=(documents,))
                embedding_thread = threading.Thread(target=run_embedding, args=(documents,))
                extraction_thread.start()
                embedding_thread.start()
                extraction_thread.join()
                embedding_thread.join()
                # After both are done, proceed with RAG extraction as before
                logger.info("Starting RAG extraction after embeddings and extraction")
                update_phase(doc_id, "rag")
                with self._rag_thread_lock:
                    try:
                        # Get the file name from the first document's metadata
                        doc_file_name = documents[0].metadata.get("file_name")
                        if not doc_file_name:
                            logger.warning("No file name found in document metadata")
                            return

                        # Get document type for company config
                        document_type = documents[0].metadata.get("document_type")
                        if not document_type:
                            logger.warning("No document type found in document metadata")
                            return

                        # Load the extraction result file
                        result_file = local_data_path / "extraction_results" / doc_id / "result.json"
                        logger.info(f"Looking for result file at: {result_file}")
                        if not result_file.exists():
                            logger.warning(f"Extraction result file not found: {result_file}")
                            return

                        with open(result_file) as f:
                            extraction_data = json.load(f)

                        extraction_result = extraction_data.get("result", {})
                        if not extraction_result:
                            logger.warning("No extraction result found in result file")
                            return

                        # Check for missing fields
                        from bridgewell_gpt.server.extraction.extraction_service import ExtractionService
                        from bridgewell_gpt.server.extraction.insurance_schema import InsuranceSummary
                        
                        # Initialize extraction service with both ingest and chat services
                        extraction_service = ExtractionService(self._ingest_service, self._chat_service)
                        missing_fields = extraction_service._has_missing_fields(extraction_result, InsuranceSummary)

                        if missing_fields:
                            logger.info(f"Found {len(missing_fields)} missing fields, using RAG to extract them")
                            # Extract missing fields using RAG with company config
                            company_config = extraction_service._load_company_config("general")  # Default to general config for now
                            rag_results = extraction_service._extract_with_rag(
                                doc_id=doc_id,
                                missing_fields=missing_fields,
                                company_config=company_config
                            )

                            # Log RAG results before updating
                            logger.info(f"RAG batch results: sections={list(rag_results.keys())}, fields={[k for sec in rag_results.values() if isinstance(sec, dict) for k in sec.keys()]}")
                            logger.info("RAG batch full results: %s", json.dumps(rag_results, indent=2))

                            # Update extraction result with RAG results
                            def deep_update(d: dict, u: dict) -> dict:
                                if not isinstance(u, dict):
                                    return d
                                for k, v in u.items():
                                    if isinstance(v, dict):
                                        # If d[k] is not a dict or is empty, replace it entirely
                                        if not isinstance(d.get(k), dict) or not d.get(k):
                                            d[k] = v
                                        else:
                                            d[k] = deep_update(d.get(k, {}), v)
                                    else:
                                        d[k] = v
                                return d

                            logger.info(f"RAG results type: {type(rag_results)}; value: {repr(rag_results)}")
                            # Only update if both extraction_result and rag_results are dicts
                            if isinstance(extraction_result, dict) and isinstance(rag_results, dict):
                                final_data = deep_update(extraction_result, rag_results)
                                extraction_data["result"] = final_data
                                with open(result_file, 'w') as f:
                                    json.dump(extraction_data, f, indent=2)
                                logger.info("Successfully updated extraction results with RAG data")
                            else:
                                logger.error(f"Cannot update extraction results: extraction_result is {type(extraction_result)}, rag_results is {type(rag_results)}")
                        else:
                            logger.info("No missing fields found, skipping RAG extraction")

                        # After RAG (or if skipped), mark as completed
                        update_phase(doc_id, "completed")
                    except Exception as e:
                        logger.error(f"Error during RAG extraction: {str(e)}")
                        update_phase(doc_id, "completed")
            except Exception as e:
                logger.error(f"Error in save task: {str(e)}")
                if doc_id is not None:
                    update_phase(doc_id, "error")
            finally:
                if doc_id is not None:
                    update_phase(doc_id, "completed")

        thread = threading.Thread(target=save_task, args=(doc_id,))
        thread.start()

    def bulk_ingest(self, files: list[tuple[str, Path]]) -> list[Document]:
        # If extraction is enabled, process files individually to handle extraction
        if self.extraction_component:
            saved_documents = []
            for file_name, file_data in files:
                docs = self.ingest(file_name, file_data)
                saved_documents.extend(docs)
            return saved_documents
        else:
            # Original parallel bulk ingestion without extraction
            documents = list(
                itertools.chain.from_iterable(
                    self._ingest_work_pool.starmap(self.ingest, files)
                )
            )
            return documents

    def _save_docs(self, documents: list[Document]) -> list[Document]:
        logger.debug("Transforming count=%s documents into nodes", len(documents))
        nodes = run_transformations(
            documents,  # type: ignore[arg-type]
            self.transformations,
            show_progress=self.show_progress,
        )
        batch_size = getattr(self.embed_model, 'embed_batch_size', 32)
        n_batches = math.ceil(len(nodes) / batch_size)
        with self._index_thread_lock:
            logger.info("Inserting count=%s nodes in the index (batch_size=%s, n_batches=%s)", len(nodes), batch_size, n_batches)
            for i, node_batch in enumerate(batch_nodes(nodes, batch_size)):
                logger.info(f"Inserting embedding batch {i+1}/{n_batches} (size {len(node_batch)})")
                self._index.insert_nodes(node_batch, show_progress=False)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            logger.debug("Persisting the index and nodes")
            self._save_index()
            logger.debug("Persisted the index and nodes")
        return documents

    def __del__(self) -> None:
        # We need to do the appropriate cleanup of the multiprocessing pools
        # when the object is deleted. Using root logger to avoid
        # the logger to be deleted before the pool
        logging.debug("Closing the ingest work pool")
        self._ingest_work_pool.close()
        self._ingest_work_pool.join()
        self._ingest_work_pool.terminate()
        logging.debug("Closing the file to documents work pool")
        self._file_to_documents_work_pool.close()
        self._file_to_documents_work_pool.join()
        self._file_to_documents_work_pool.terminate()


class PipelineIngestComponent(BaseIngestComponentWithIndex):
    """Pipeline ingestion - keeping the embedding worker pool as busy as possible.

    This class implements a threaded ingestion pipeline, which comprises two threads
    and two queues. The primary thread is responsible for reading and parsing files
    into documents. These documents are then placed into a queue, which is
    distributed to a pool of worker processes for embedding computation. After
    embedding, the documents are transferred to another queue where they are
    accumulated until a threshold is reached. Upon reaching this threshold, the
    accumulated documents are flushed to the document store, index, and vector
    store.

    Exception handling ensures robustness against erroneous files. However, in the
    pipelined design, one error can lead to the discarding of multiple files. Any
    discarded files will be reported.
    """

    NODE_FLUSH_COUNT = 5000  # Save the index every # nodes.

    def __init__(
        self,
        storage_context: StorageContext,
        embed_model: EmbedType,
        transformations: list[TransformComponent],
        count_workers: int,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(storage_context, embed_model, transformations, *args, **kwargs)
        self.count_workers = count_workers
        assert (
            len(self.transformations) >= 2
        ), "Embeddings must be in the transformations"
        assert count_workers > 0, "count_workers must be > 0"
        self.count_workers = count_workers
        # We are doing our own multiprocessing
        # To do not collide with the multiprocessing of huggingface, we disable it
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        # doc_q stores parsed files as Document chunks.
        # Using a shallow queue causes the filesystem parser to block
        # when it reaches capacity. This ensures it doesn't outpace the
        # computationally intensive embeddings phase, avoiding unnecessary
        # memory consumption.  The semaphore is used to bound the async worker
        # embedding computations to cause the doc Q to fill and block.
        self.doc_semaphore = multiprocessing.Semaphore(
            self.count_workers
        )  # limit the doc queue to # items.
        self.doc_q: Queue[tuple[str, str | None, list[Document] | None]] = Queue(20)
        # node_q stores documents parsed into nodes (embeddings).
        # Larger queue size so we don't block the embedding workers during a slow
        # index update.
        self.node_q: Queue[
            tuple[str, str | None, list[Document] | None, list[BaseNode] | None]
        ] = Queue(40)
        threading.Thread(target=self._doc_to_node, daemon=True).start()
        threading.Thread(target=self._write_nodes, daemon=True).start()

    def _doc_to_node(self) -> None:
        # Parse documents into nodes
        with multiprocessing.pool.ThreadPool(processes=self.count_workers) as pool:
            while True:
                try:
                    cmd, file_name, documents = self.doc_q.get(
                        block=True
                    )  # Documents for a file
                    if cmd == "process":
                        # Push CPU/GPU embedding work to the worker pool
                        # Acquire semaphore to control access to worker pool
                        self.doc_semaphore.acquire()
                        pool.apply_async(
                            self._doc_to_node_worker, (file_name, documents)
                        )
                    elif cmd == "quit":
                        break
                finally:
                    if cmd != "process":
                        self.doc_q.task_done()  # unblock Q joins

    def _doc_to_node_worker(self, file_name: str, documents: list[Document]) -> None:
        # CPU/GPU intensive work in its own process
        try:
            nodes = run_transformations(
                documents,  # type: ignore[arg-type]
                self.transformations,
                show_progress=self.show_progress,
            )
            self.node_q.put(("process", file_name, documents, list(nodes)))
        finally:
            self.doc_semaphore.release()
            self.doc_q.task_done()  # unblock Q joins

    def _save_docs(
        self, files: list[str], documents: list[Document], nodes: list[BaseNode]
    ) -> None:
        try:
            logger.info(
                f"Saving {len(files)} files ({len(documents)} documents / {len(nodes)} nodes)"
            )
            self._index.insert_nodes(nodes)
            for document in documents:
                self._index.docstore.set_document_hash(
                    document.get_doc_id(), document.hash
                )
            self._save_index()
        except Exception:
            # Tell the user so they can investigate these files
            logger.exception(f"Processing files {files}")
        finally:
            # Clearing work, even on exception, maintains a clean state.
            nodes.clear()
            documents.clear()
            files.clear()

    def _write_nodes(self) -> None:
        # Save nodes to index.  I/O intensive.
        node_stack: list[BaseNode] = []
        doc_stack: list[Document] = []
        file_stack: list[str] = []
        while True:
            try:
                cmd, file_name, documents, nodes = self.node_q.get(block=True)
                if cmd in ("flush", "quit"):
                    if file_stack:
                        self._save_docs(file_stack, doc_stack, node_stack)
                    if cmd == "quit":
                        break
                elif cmd == "process":
                    node_stack.extend(nodes)  # type: ignore[arg-type]
                    doc_stack.extend(documents)  # type: ignore[arg-type]
                    file_stack.append(file_name)  # type: ignore[arg-type]
                    # Constant saving is heavy on I/O - accumulate to a threshold
                    if len(node_stack) >= self.NODE_FLUSH_COUNT:
                        self._save_docs(file_stack, doc_stack, node_stack)
            finally:
                self.node_q.task_done()

    def _flush(self) -> None:
        self.doc_q.put(("flush", None, None))
        self.doc_q.join()
        self.node_q.put(("flush", None, None, None))
        self.node_q.join()

    def ingest(self, file_name: str, file_data: Path) -> list[Document]:
        documents = IngestionHelper.transform_file_into_documents(file_name, file_data)
        self.doc_q.put(("process", file_name, documents))
        self._flush()
        return documents

    def bulk_ingest(self, files: list[tuple[str, Path]]) -> list[Document]:
        docs = []
        for file_name, file_data in eta(files):
            try:
                documents = IngestionHelper.transform_file_into_documents(
                    file_name, file_data
                )
                self.doc_q.put(("process", file_name, documents))
                docs.extend(documents)
            except Exception:
                logger.exception(f"Skipping {file_data.name}")
        self._flush()
        return docs


def get_ingestion_component(
    storage_context: StorageContext,
    embed_model: EmbedType,
    transformations: list[TransformComponent],
    settings: Settings,
    ingest_service: Any = None,  # Type hint as Any to avoid circular import
    chat_service: Any = None,  # Add chat service parameter
) -> BaseIngestComponent:
    """Get the ingestion component for the given configuration."""
    ingest_mode = settings.embedding.ingest_mode
    if ingest_mode == "batch":
        return BatchIngestComponent(
            storage_context=storage_context,
            embed_model=embed_model,
            transformations=transformations,
            count_workers=settings.embedding.count_workers,
        )
    elif ingest_mode == "parallel":
        return ParallelizedIngestComponent(
            storage_context=storage_context,
            embed_model=embed_model,
            transformations=transformations,
            count_workers=settings.embedding.count_workers,
            extraction_component=ExtractionComponent(),
            ingest_service=ingest_service,
            chat_service=chat_service,
        )
    elif ingest_mode == "pipeline":
        return PipelineIngestComponent(
            storage_context=storage_context,
            embed_model=embed_model,
            transformations=transformations,
            count_workers=settings.embedding.count_workers,
        )
    else:
        return SimpleIngestComponent(
            storage_context=storage_context,
            embed_model=embed_model,
            transformations=transformations,
            extraction_component=ExtractionComponent(),
            ingest_service=ingest_service,
            chat_service=chat_service,
        )

# Helper to batch nodes

def batch_nodes(nodes, batch_size):
    for i in range(0, len(nodes), batch_size):
        yield nodes[i:i+batch_size]
