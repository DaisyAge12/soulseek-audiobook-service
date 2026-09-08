from app.worker import flat,fk
def test_flat():
 x=list(flat([{'directories':[{'files':[{'filename':'a.m4b','state':'Completed','percentComplete':100}]}]}]));assert len(x)==1 and fk(x[0]['filename'])=='a.m4b'
