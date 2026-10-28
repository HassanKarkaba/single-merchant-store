import { useState } from 'react';
import { Product, Variant } from '../../controllers/browser/contracts';
import { Artwork } from './artwork';
import { Icon, NavLink } from './common';
import { money, titleCase } from '../presentation/format';
export function ProductCard({ product, index, navigate, onAdd }: {
    product: Product;
    index: number;
    navigate: (path: string) => void;
    onAdd: (variant: Variant) => void;
}) { const variant = product.variants[0]; return <article className="product-card"><div className="product-image"><NavLink href={`/products/${product.slug}`} navigate={navigate} className="pf-style-30"><ProductImage product={product} index={index}/></NavLink>{index === 0 && <span className="product-badge">An everyday favorite</span>}{!(variant?.available > 0) ? <span className="product-badge">Currently unavailable</span> : variant && <button className="quick-add" aria-label={`Add ${product.name} to your bag`} onClick={() => onAdd(variant)}><Icon name="plus" size={14}/></button>}</div><NavLink href={`/products/${product.slug}`} navigate={navigate}><div className="product-meta"><h3>{product.name}</h3><span>{money(variant?.unit_minor)}</span></div><p className="product-sub">{titleCase(product.category || 'Considered essentials')}</p></NavLink></article>; }
export function ProductGallery({ product, index = 0, ready = false }: {
    product: Product;
    index?: number;
    ready?: boolean;
}) {
    const [selected, setSelected] = useState(0);
    const images = product.images?.length ? product.images : product.media || [];
    const count = Math.min(images.length, 10), active = Math.min(selected, Math.max(0, count - 1));
    return <section aria-label={`${product.name} images`}><div className="product-stage"><ProductImage product={product} index={index} imageIndex={active}/><span className="product-badge">Synthetic product illustration</span></div>{count > 1 && <div className="option-buttons" role="group" aria-label="Product image gallery">{images.slice(0, count).map((_, position) => <button key={position} type="button" aria-label={`Show product image ${position + 1}`} disabled={!ready} aria-pressed={position === active} onClick={() => setSelected(position)}>Image {position + 1}</button>)}</div>}</section>;
}
export function ProductImage({ product, index = 0, imageIndex = 0 }: {
    product: Product;
    index?: number;
    imageIndex?: number;
}) { const [failedUrl, setFailedUrl] = useState(''); const first = product.images?.[imageIndex] || product.media?.[imageIndex]; const url = typeof first === 'string' ? first : first?.url; const local = typeof url === 'string' && /^\/assets\/[a-zA-Z0-9/_.-]+$/.test(url) && !url.includes('..'); const alt = typeof first === 'object' ? first.alt_text || `${product.name}, synthetic catalog object` : `${product.name}, synthetic catalog object`; return local && failedUrl !== url ? <img className="curated-product-image" src={url} alt={alt} width={360} height={390} loading="lazy" onError={() => setFailedUrl(String(url))}/> : <><Artwork name={product.name} index={index}/><span className="sr-only">Curated image unavailable; synthetic illustration shown.</span></>; }
