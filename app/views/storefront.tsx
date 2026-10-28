import './styles/storefront.css';
import { bootstrapBrowser } from '../controllers/browser/bootstrap';
export { ApiError, BrowserApiClient, BrowserRequestStore, normalizeProducts, validateMoney, validateSessionDTO } from '../controllers/browser';
export { money } from './presentation/format';
export { App } from './App';
export { renderApp, renderPage } from './rendering';
if (typeof window !== 'undefined' && typeof document !== 'undefined' && document.getElementById('root'))
    bootstrapBrowser();

export {parseAccountActionResult, validateAccountActionInput} from '../controllers/browser/account-actions';
export {parseAccountLink} from '../controllers/browser/account-links';
export {validateAccountFields} from '../controllers/browser/account-validation';

export {ResetIntentIdentity} from '../controllers/browser/reset-intent';
export {accountFieldErrorsFromApi} from '../controllers/browser/account-validation';

export {isAdministrator} from '../controllers/browser/account-navigation';
