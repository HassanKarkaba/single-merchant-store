import type { ReactNode } from 'react';
import { InitialData } from '../controllers/browser/contracts';
import { useStorefrontController } from '../controllers/browser/hooks/storefront';
import { ErrorSummary, Icon, Loading, NavLink, PageHead } from './components/common';
import { StoreFooter, StoreHeader } from './components/layout';
import { AccountAccessScreen } from './pages/account-access';
import { AccountAccessMode } from '../controllers/browser/hooks/account-access';
import { AccountScreen } from './pages/account';
import { CartScreen } from './pages/cart';
import { CatalogScreen } from './pages/catalog';
import { CheckoutScreen } from './pages/checkout';
import { HomeScreen } from './pages/home';
import { MerchantScreen } from './pages/merchant';
import { OperationScreen } from './pages/operation';
import { OrderScreen } from './pages/order';
import { PolicyScreen } from './pages/policy';
import { ProductScreen } from './pages/product';
import { PurchaseScreen } from './pages/purchase';
import { ReturnScreen } from './pages/return';
export function App({ path: initialPath = '/', initialData = {} }: {
    path?: string;
    initialData?: InitialData;
}) {
    const { path, products, categories, cart, session, notice, setNotice, cartError, catalogError, catalogLoading, cartPending, client, notify, navigate, refreshCart, refreshSession, add } = useStorefrontController(initialPath, initialData);
    const common = { client, navigate, notify, session, cart, cartPending, refreshCart, refreshSession, products, categories };
    const basePath = path.split('?')[0], isMerchant = basePath.startsWith('/merchant');
    const accountAccess = ['register','verify-email','forgot-password','reset-password'].includes(basePath.slice(1)) ? basePath.slice(1) as AccountAccessMode : null;
    let content: ReactNode;
    if (accountAccess)
        content = <AccountAccessScreen key={accountAccess} {...common} mode={accountAccess}/>;
    else if (isMerchant)
        content = <MerchantScreen {...common} path={basePath}/>;
    else if (basePath.startsWith('/products/'))
        content = <ProductScreen key={path} {...common} slug={decodeURIComponent(basePath.split('/')[2] || '')} variantId={new URLSearchParams(path.split('?')[1] || '').get('variant')} onAdd={add}/>;
    else if (basePath === '/cart')
        content = <CartScreen {...common}/>;
    else if (basePath.startsWith('/checkout/'))
        content = <CheckoutScreen {...common} id={basePath.split('/')[2]}/>;
    else if (basePath.startsWith('/purchases/'))
        content = <PurchaseScreen {...common} id={basePath.split('/')[2]}/>;
    else if (basePath.startsWith('/operations/'))
        content = <OperationScreen {...common} id={basePath.split('/')[2]}/>;
    else if (basePath.startsWith('/orders/') || basePath === '/orders' || basePath === '/account/orders')
        content = <OrderScreen {...common} id={basePath.startsWith('/orders/') ? basePath.split('/')[2] : null}/>;
    else if (basePath.startsWith('/returns/'))
        content = <ReturnScreen {...common} id={basePath.split('/')[2]}/>;
    else if (basePath.startsWith('/account'))
        content = <AccountScreen {...common} path={basePath}/>;
    else if (basePath === '/search' || basePath.startsWith('/categories/'))
        content = <CatalogScreen {...common} path={path} loading={catalogLoading} error={catalogError} onAdd={add}/>;
    else if (basePath === '/policies' || basePath === '/about')
        content = <PolicyScreen navigate={navigate} about={basePath === '/about'}/>;
    else if (basePath === '/')
        content = <HomeScreen {...common} loading={catalogLoading} error={catalogError} onAdd={add}/>;
    else
        content = <div className="container"><PageHead title="This page is unavailable."/><NavLink href="/search" navigate={navigate} className="button">Return to the collection</NavLink></div>;
    const privatePath = isMerchant || /^\/(?:cart|checkout|purchases|orders|returns|operations)(?:\/|$)/.test(basePath) || /^\/account\/(?:orders|privacy)$/.test(basePath);
    if (privatePath && !session)
        content = <Loading label="Checking your current identity"/>;
    else if (privatePath && !session?.actor)
        content = <AccountScreen {...common}/>;
    const key = `${session?.actor?.id || 'preauth'}:${session?.epoch || 'loading'}:${session?.session_generation || 0}`;
    return <><a className="skip" href="#main">Skip to content</a><div className="sandbox">Portfolio demo · Synthetic data · Simulated payment, tax, delivery & order messages <NavLink href="/account" navigate={navigate}>Reviewer sign in</NavLink></div>{!isMerchant && <StoreHeader navigate={navigate} cart={cart} session={session}/>}<main id="main"><div key={accountAccess || (basePath === '/account' ? 'account' : key)} data-session-scope={session?.actor ? 'authenticated' : 'preauth'}>{cartError && <div className="container"><ErrorSummary message={cartError}/></div>}{content}</div></main>{!isMerchant && <StoreFooter navigate={navigate}/>}<div id="native-ui-island" className="native-island"/>{notice && <div role="status" className="toast"><span>{notice}</span><button aria-label="Dismiss notification" onClick={() => setNotice('')}><Icon name="close" size={14}/></button></div>}</>;
}
