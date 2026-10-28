import { Dict } from '../../controllers/browser/contracts';
import { useOperationController } from '../../controllers/browser/hooks/operation';
import { ErrorSummary, Icon, Loading, NavLink, PageHead, SafeRecord, StatusBadge } from '../components/common';
export function OperationScreen(props: Dict) {
    const { navigate, record, error, retryStatus, refreshStatus } = useOperationController(props);
    return <div className="container"><PageHead title="Your operation, accounted for." subtitle="A lost reply keeps its original identity. Current access is checked again here."/><div className="account-layout">{error ? <ErrorSummary message={error} retry={retryStatus()}/> : !record ? <Loading label="Checking the original operation"/> : <div className="panel"><h2>Current owner status</h2><StatusBadge state={record.state || record.outcome}/><SafeRecord record={record}/>{record.order_id && <NavLink href={`/orders/${record.order_id}`} navigate={navigate} className="button pf-style-23">View order <Icon name="arrow"/></NavLink>}<button className="button secondary pf-style-23" onClick={refreshStatus()}>Refresh current status <Icon name="refresh"/></button></div>}</div></div>;
}
