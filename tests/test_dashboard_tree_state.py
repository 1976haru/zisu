import unittest

from mcrs.dashboard_tree_state import restore, snapshot


class FakeTree:
    def __init__(self):
        self.nodes={"account": {"parent":"","open":True},
                    "channel": {"parent":"account","open":False},
                    "profile": {"parent":"channel","open":True},
                    "other": {"parent":"","open":True}}
        self.selected=["profile"]
        self.focused="profile"
        self.position=(0.4,0.8)

    def get_children(self,parent):
        return tuple(key for key,value in self.nodes.items() if value["parent"]==parent)

    def item(self,iid,option=None,**kwargs):
        if kwargs:self.nodes[iid].update(kwargs)
        return self.nodes[iid].get(option) if option else dict(self.nodes[iid])

    def selection(self):return tuple(self.selected)
    def selection_set(self,*items):self.selected=list(items)
    def focus(self,item=None):
        if item is not None:self.focused=item
        return self.focused
    def yview(self):return self.position
    def yview_moveto(self,value):self.position=(value,min(1.0,value+0.4))
    def exists(self,item):return item in self.nodes
    def delete(self,*items):
        for item in items:
            self.nodes.pop(item,None)


class DashboardTreeStateTests(unittest.TestCase):
    def test_refresh_restores_expansion_selection_focus_and_scroll(self):
        before=FakeTree();before.nodes["channel"]["open"]=True;before.nodes["other"]["open"]=False
        state=snapshot(before)
        after=FakeTree()
        after.selected=[];after.focused=""
        restore(after,state,"other")
        self.assertEqual(("profile",),after.selection())
        self.assertEqual("profile",after.focus())
        self.assertTrue(after.item("account","open"))
        self.assertTrue(after.item("channel","open"))
        self.assertTrue(after.item("profile","open"))
        self.assertFalse(after.item("other","open"))
        self.assertEqual(0.4,after.yview()[0])

    def test_deleted_selection_falls_back_to_active_profile_and_ignores_stale_open_nodes(self):
        tree=FakeTree();state=snapshot(tree)
        replacement=FakeTree();replacement.delete("profile")
        restore(replacement,state,"other")
        self.assertEqual(("other",),replacement.selection())
        self.assertEqual("other",replacement.focus())


if __name__=="__main__":unittest.main()
