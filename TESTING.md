# Souffl.AI Test Suite

## Overview

This directory contains comprehensive unit and integration tests for the Souffl.AI project — a Telegram bot that enables artisans to send voice memos and receive professional PDF quotes (devis).

## Test Results

**109 tests passing** across 5 test modules.

All tests run successfully with:
```bash
cd /sessions/modest-adoring-cray/mnt/Souffl.AI\ -\ Devis\ AI\ Vocal/
python3 -m pytest tests/ -v
```

## Test Structure

### 1. test_calculs.py (23 tests)

Tests for `calculs.py` - the deterministic financial calculation engine.

**Classes:**
- `TestRecalculerTotaux` (13 tests)
  - `test_empty_devis` - handles empty devis
  - `test_single_line_tva_*` - tests for TVA 0%, 5.5%, 10%, 20%
  - `test_multiple_lines_mixed_tva` - mixed TVA rates
  - `test_rounding_precision` - ensures 2-decimal precision
  - `test_deepcopy_no_mutation` - verifies original dict not modified
  - `test_default_acompte_pourcentage` - default 30% acompte
  - `test_zero_quantity` / `test_zero_price` - edge cases
  - `test_custom_acompte_pourcentage` - variable acompte rates

- `TestAppliquerTauxTvaUniforme` (7 tests)
  - `test_tva_zero_mentions_legales` - TVA 0 adds legal notes
  - `test_tva_*_applied_uniformly` - applies TVA to all lines
  - `test_deepcopy_no_mutation_uniform` - no mutation guarantee
  - `test_empty_lines_uniform_tva` - handles empty devis

- `TestEdgeCases` (3 tests)
  - `test_very_small_values` - precision with micro amounts
  - `test_large_values` - large quantities/prices
  - `test_floating_point_precision` - floating point rounding

**Coverage:**
- TVA calculation with multiple rates (0%, 5.5%, 10%, 20%)
- Line item montant calculation: quantite × prix_unitaire_ht
- Sous-total HT and TVA amounts
- Total TTC (all-inclusive)
- Acompte and solde calculations
- Rounding to 2 decimal places
- No mutation of input data

### 2. test_client_store.py (20 tests)

Tests for `client_store.py` - artisan profile management (multi-tenant).

**Classes:**
- `TestClientStore` (10 tests)
  - `test_save_and_get_client` - CRUD operations
  - `test_client_exists` - existence check
  - `test_delete_client` - deletion
  - `test_multiple_clients` - multi-artisan support
  - `test_empty_profile` - handles empty profiles
  - `test_profile_with_missing_fields` - partial profiles
  - `test_list_clients` - list all artisans
  - `test_nonexistent_client_returns_none` - safe missing clients
  - `test_profile_persistence` - JSON file persistence
  - `test_special_characters_in_profile` - unicode/accents (François, Côte d'Azur, etc.)

- `TestInjectProfileInPrompt` (8 tests)
  - `test_basic_replacement` - placeholder substitution
  - `test_missing_profile_fields` - uses [À COMPLÉTER] for missing fields
  - `test_custom_tarifs_replacement` - personalised rates
  - `test_deplacement_replacement` - travel costs
  - `test_all_placeholders` - all 13 placeholders
  - `test_none_values_not_replaced` - None values skip replacement
  - `test_empty_string_values_not_replaced` - empty strings skip replacement

- `TestEmptyProfile` (2 tests)
  - `test_empty_profile_structure` - valid dict returned
  - `test_empty_profile_has_defaults` - proper defaults

**Coverage:**
- Profile CRUD (Create, Read, Update, Delete)
- Existence checks
- Multi-tenant isolation
- File-based persistence
- Placeholder injection for LLM system prompt
- 13 different profile fields (SIRET, IBAN, TVA, rates, etc.)
- Special characters and unicode

### 3. test_devis_store.py (17 tests)

Tests for `devis_store.py` - devis and facture persistence.

**Classes:**
- `TestDevisStore` (8 tests)
  - `test_save_devis` - saves JSON to file
  - `test_load_devis` - retrieves devis by number
  - `test_load_devis_not_found` - handles missing devis
  - `test_list_devis` - lists recent devis
  - `test_list_devis_order` - most recent first
  - `test_get_last_devis` - retrieves latest devis
  - `test_get_last_devis_empty` - handles no devis
  - `test_numero_with_special_characters` - safe file naming

- `TestFactureStore` (9 tests)
  - `test_save_facture` - saves facture JSON
  - `test_load_facture` - retrieves by invoice number
  - `test_load_facture_not_found` - handles missing
  - `test_get_factures_for_devis` - lists related invoices
  - `test_get_factures_for_devis_empty` - handles no invoices
  - `test_get_total_deja_facture` - sums invoiced amounts
  - `test_get_total_deja_facture_empty` - returns 0 when none
  - `test_get_total_deja_facture_rounding` - proper rounding
  - `test_list_devis_with_missing_files` - gracefully handles corrupted JSON

**Coverage:**
- Devis CRUD operations
- Facture (invoice) CRUD operations
- Multi-user (multi-artisan) file organization
- Devis listing and filtering
- Invoice tracking per devis
- Total invoiced amount calculation
- File safety with special character handling
- Error resilience with corrupted files

### 4. test_bot_helpers.py (37 tests)

Tests for pure helper functions in `bot.py` (no async, no external APIs).

**Classes:**
- `TestIsSkip` (8 tests)
  - `test_skip_button_text` - "⏭️ Passer" button
  - `test_non_button_text` - "❌ Non, c'est bon" button
  - `test_skip_words` - 14 skip variants (passer, plus tard, skip, non, nope, rien, etc.)
  - `test_case_insensitive_skip` - uppercase/mixed case
  - `test_with_spaces` - trimming
  - `test_non_skip_text` - negative cases
  - `test_empty_string` - edge case
  - `test_partial_skip_words` - no false matches (surplus, non-compris)

- `TestBuildUserMessage` (8 tests)
  - `test_basic_message` - core message structure
  - `test_validity_date_calculation` - today + 90 days
  - `test_with_client_extra` - client coordinates injection
  - `test_with_duree_extra` - work duration
  - `test_with_marques_extra` - equipment brands
  - `test_with_prix_postes_extra` - custom prices
  - `test_with_all_extras` - all parameters combined
  - `test_none_extra` - graceful null handling

- `TestDetecterTvaZero` (14 tests)
  - `test_non_assujetti` - "non assujetti à la TVA"
  - `test_pas_assujetti` - "pas assujetti"
  - `test_sans_tva` / `test_pas_de_tva` - "without TVA"
  - `test_tva_zero_patterns` - "tva 0", "0% tva", "0 % tva"
  - `test_exempt_patterns` - "exempté", "exempt"
  - `test_franchise_en_base` - threshold exemption
  - `test_micro_entreprise_patterns` - "micro-entreprise", "auto-entrepreneur"
  - `test_article_293` - "art 293", "article 293"
  - `test_hors_tva` / `test_ht_seulement` - "outside VAT", "HT only"
  - `test_case_insensitive` - uppercase handling
  - `test_no_false_positives` - "assujetti à la TVA" does NOT trigger (critical)
  - `test_empty_string` - edge case
  - `test_embedded_patterns` - patterns within text
  - `test_accent_insensitive` - accent handling

- `TestBuildRecap` (7 tests)
  - `test_basic_recap` - summary message generation
  - `test_recap_with_flags` - warning flags display
  - `test_recap_empty_or_invalid_flags` - filter empty/ellipsis flags
  - `test_recap_missing_fields` - handle missing data gracefully
  - `test_recap_markdown_formatting` - uses markdown (bold, code)
  - `test_recap_numbers_formatting` - amount formatting
  - `test_recap_zero_values` - handles zero amounts

**Coverage:**
- User message preprocessing
- Skip detection (14 variants)
- TVA zero detection (11 patterns + false positive prevention)
- Devis recap generation
- Markdown formatting
- Date calculations
- Edge cases and missing data

### 5. test_integration.py (12 tests)

End-to-end workflow tests combining multiple modules.

**Classes:**
- `TestIntegrationDevisWorkflow` (3 tests)
  - `test_devis_recalculation_workflow` - full calculation flow
  - `test_devis_tva_zero_workflow` - TVA zero → legal notes
  - `test_complex_multi_tva_devis` - 4 lines with mixed TVA rates

- `TestIntegrationFactureWorkflow` (6 tests)
  - `test_devis_to_simple_facture` - basic conversion
  - `test_devis_to_facture_typed_acompte` - 30% down payment
  - `test_devis_to_facture_typed_solde` - final balance
  - `test_devis_to_facture_typed_intermediaire` - work in progress invoice
  - `test_facture_sequence_acompte_inter_solde` - full sequence
  - `test_facture_missing_iban_placeholder` - safety fallback

- `TestIntegrationCrossModuleConsistency` (3 tests)
  - `test_calculs_consistency_with_pdf_data` - data availability
  - `test_rounding_consistency` - all amounts follow 2-decimal rule
  - `test_devis_facture_totaux_preserved` - totals survive conversion

**Coverage:**
- Complete devis creation and calculation flow
- Devis to facture conversion
- Typed invoice generation (acompte/intermediaire/solde)
- Full payment sequence tracking
- Cross-module data integrity
- Rounding consistency across all calculations

## Test Fixtures and Mocking

### Temporary Files
- `tmp_path` fixture for JSON file tests
- Temporary directories for devis/client storage
- No pollution of real data files

### External Dependencies
Tests mock or isolate:
- OpenAI API (not called)
- Telegram API (not called)
- SMTP (email sending not tested)
- File system isolation with temporary directories

## Running the Tests

### Run all tests:
```bash
python3 -m pytest tests/ -v
```

### Run specific test file:
```bash
python3 -m pytest tests/test_calculs.py -v
```

### Run specific test class:
```bash
python3 -m pytest tests/test_calculs.py::TestRecalculerTotaux -v
```

### Run specific test:
```bash
python3 -m pytest tests/test_calculs.py::TestRecalculerTotaux::test_rounding_precision -v
```

### Run with coverage:
```bash
python3 -m pip install pytest-cov
python3 -m pytest tests/ --cov=. --cov-report=html
```

## Key Testing Insights

### Financial Calculations (calculs.py)
- All monetary amounts rounded to 2 decimals
- TVA calculation: per rate, not mixed
- Acompte/solde: calculated as % of total TTC
- No mutation of input devis dict (deepcopy)

### Profile Management (client_store.py)
- 13 placeholder fields can be injected into LLM system prompt
- Handles special characters (accents, symbols)
- Missing fields replaced with [À COMPLÉTER]
- None/empty string values skip replacement

### Data Persistence (devis_store.py)
- Per-user directories: data/devis/{user_id}/
- JSON files named by devis/facture number (special chars sanitized)
- Graceful handling of corrupted JSON
- Multi-tenant isolation by user_id

### Bot Helpers (bot.py)
- TVA zero detection with 11 patterns and false positive prevention
- Skip button detection: 14 text variants + case insensitivity
- User message builder: injects transcription + metadata + optional extras
- Recap generation: markdown formatted with amounts and flags

### Integration Tests
- Full workflows from devis creation through invoice generation
- Acompte → situation intermédiaire → solde sequences
- Data preservation through conversions
- Rounding consistency across modules

## Test Quality

- **109 tests** covering core functionality
- **5 test modules** organized by responsibility
- **Deterministic** - no flakiness, no randomness
- **Isolated** - no external dependencies, no network
- **Fast** - complete suite runs in ~1.6 seconds
- **Well-documented** - docstrings and comments explain intent
- **Edge cases** - tests for zeros, missing fields, special characters
- **Error paths** - tests for missing files, invalid JSON

## Future Test Expansion

Areas not covered (by design):
- Async functions in bot.py (require separate async test framework)
- LLM integration (requires mocking OpenAI)
- PDF generation (requires WeasyPrint with all dependencies)
- Telegram API interactions
- Email sending (SMTP)
- Actual speech-to-text (Whisper API)

These could be added with:
- `pytest-asyncio` for async handler tests
- `unittest.mock` for API mocking
- Integration tests with test fixtures/VCR cassettes
