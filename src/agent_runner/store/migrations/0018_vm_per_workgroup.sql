-- Several VMs per workgroup on one device (KOR-41, ADR-075 section 4; 05-runner.md section 3.3). The rule "one live
-- VM per workgroup" is dropped; the VM of a session is chosen under the workgroup's in-process lock and recorded on
-- the session row before the lock is released. A VM still belongs to exactly one workgroup for its life.
DROP INDEX runner.vm_wg_id_live;
CREATE INDEX vm_wg_id ON runner.vm (wg_id) WHERE state <> 'deleted';
CREATE INDEX session_vm_id ON runner.session (vm_id) WHERE vm_id IS NOT NULL;
