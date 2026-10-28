import type {Session} from './contracts';
export function isAdministrator(session:Session|null|undefined): boolean {
    return session?.actor?.kind==='staff' && session.actor.roles.includes('merchant_admin');
}
