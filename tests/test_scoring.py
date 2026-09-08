from app.scoring import group,rank
from app.schemas import RequestIn
def test_rank():
 r=[{'username':'p','hasFreeUploadSlot':True,'files':[{'filename':'Books\\Andy Weir\\Project Hail Mary\\book.m4b','size':700000000},{'filename':'Books\\Andy Weir\\Project Hail Mary\\cover.jpg','size':1000}]}]
 c=rank(group(r),RequestIn(external_request_id='1',title='Project Hail Mary',author='Andy Weir'));assert len(c)==1 and len(c[0].required_files)==1 and len(c[0].optional_files)==1 and c[0].score>35
